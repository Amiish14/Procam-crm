"""
CRM-VALUE-01 — a lead does not go past RFQ without a number on it.

912 of 945 open leads carry no value of any kind, so every pipeline
total, every forecast and the win rate are floors rather than
figures. Reporting does not fix that; the number has to be asked for
at the moment somebody knows it.

The line is RFQ Generated. Before it, a lead is a conversation and
pricing it invents data. From it, somebody has asked us for a price.
Lost, On Hold and Not Interested are exempt on purpose: a deal can
be lost before it was ever priced, and refusing to record that would
trap the lead open forever.
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'valuegate-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'ValueGateTest12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'valuegate.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Lead, Employee = _main.Lead, _main.Employee
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.access import scope as sc_mod                    # noqa: E402
from app.models.access import AccessProfile, DataScope    # noqa: E402
from app.services import value_gate                       # noqa: E402
from app.workbench import filters as wf                   # noqa: E402
from app.workbench import service as wb                   # noqa: E402

ADMIN, REP = 'VGADMIN', 'VGREP'
TAG = 'VG '


def _emp(code, name, scope=DataScope.OWN, admin=False):
    e = (Employee.query.filter_by(emp_code=code).first()
         or Employee(emp_code=code))
    e.name, e.is_active, e.must_change_pw = name, True, False
    e.role = 'admin' if admin else 'user'
    e.is_super_admin, e.session_version = admin, 0
    e.vertical, e.email = 'Freight Forwarding', f'{code.lower()}@procamgroup.in'
    db.session.add(e)
    db.session.flush()
    AccessProfile.query.filter_by(emp_code=code).delete()
    db.session.add(AccessProfile(emp_code=code, data_scope=scope, perms=[]))
    return e


def _lead(name, stage='Call Done', value=None, **kw):
    l = Lead(company=TAG + name, assigned_to=REP, assigned_name=REP,
             stage=stage, procam_vertical='Freight Forwarding',
             created_at=datetime.utcnow() - timedelta(days=5))
    if value is not None:
        l.estimated_value_inr = value
    for k, v in kw.items():
        setattr(l, k, v)
    db.session.add(l)
    return l


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        old = [l.id for l in Lead.query.filter(Lead.company.like(TAG + '%'))]
        if old:
            Lead.query.filter(Lead.id.in_(old)).delete(
                synchronize_session=False)
        _emp(ADMIN, 'Value Admin', scope=DataScope.ALL, admin=True)
        _emp(REP, 'Value Rep')
        db.session.flush()
        rows = {
            'bare': _lead('Bare Ltd', stage='Call Done'),
            'priced': _lead('Priced Ltd', stage='Call Done', value=500000),
            'past_rfq': _lead('Past RFQ Ltd', stage='RFQ Generated'),
            'quoted_ok': _lead('Quoted OK Ltd', stage='Quoted',
                               value=250000),
        }
        db.session.commit()
        wb.hygiene_cache_clear()
        yield {k: v.id for k, v in rows.items()}


def _client(code):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role='user', sv=0)
    return c


# ── where the line is ────────────────────────────────────────────────
def test_the_line_is_rfq_generated_and_won():
    assert value_gate.REQUIRES_VALUE == (
        'RFQ Generated', 'Quoted', 'Under Negotiation', 'Won')


@pytest.mark.parametrize('stage', ['New', 'Call Done', 'Profile Sent',
                                   'Business Discussion', 'Appointment',
                                   'Visit Done'])
def test_early_stages_ask_for_nothing(stage):
    assert not value_gate.requires_value(stage)


@pytest.mark.parametrize('stage', ['Lost', 'On Hold', 'Not Interested'])
def test_a_deal_can_be_lost_before_it_was_ever_priced(stage):
    """Exempt on purpose. Refusing a Lost because nobody priced it
    leaves the lead open forever, which is worse than the gap."""
    assert not value_gate.requires_value(stage)


# ── the stepper ──────────────────────────────────────────────────────
def test_advancing_past_rfq_without_a_value_is_refused(world):
    r = _client(ADMIN).post(f'/api/leads/{world["bare"]}/advance',
                            json={'to_stage': 'RFQ Generated'})
    assert r.status_code == 400
    body = r.get_json()
    assert body['needs'] == 'estimated_value'
    with flask_app.app_context():
        lead = db.session.get(Lead, world['bare'])
        assert lead.stage == 'Call Done', 'the move must not have landed'


def test_a_refused_move_leaves_no_stage_history_behind(world):
    from app import LeadStageHistory
    _client(ADMIN).post(f'/api/leads/{world["bare"]}/advance',
                        json={'to_stage': 'RFQ Generated'})
    with flask_app.app_context():
        assert LeadStageHistory.query.filter_by(
            lead_id=world['bare']).count() == 0


def test_a_value_sent_with_the_move_lets_it_through(world):
    r = _client(ADMIN).post(f'/api/leads/{world["bare"]}/advance',
                            json={'to_stage': 'RFQ Generated',
                                  'estimated_value': '750000'})
    assert r.status_code == 200, r.get_json()
    with flask_app.app_context():
        lead = db.session.get(Lead, world['bare'])
        assert lead.stage == 'RFQ Generated'


def test_the_value_sent_with_the_move_is_actually_stored(world):
    """The gap that would have made the whole thing pointless: a gate
    satisfied by a number that is then thrown away moves the lead and
    leaves it valueless, which is the bug the gate exists to stop."""
    from app.services import lead_value
    _client(ADMIN).post(f'/api/leads/{world["bare"]}/advance',
                        json={'to_stage': 'RFQ Generated',
                              'estimated_value': '750000'})
    with flask_app.app_context():
        lead = db.session.get(Lead, world['bare'])
        assert lead_value.value_inr(lead) is not None, \
            'the lead moved and the value vanished'
        assert float(lead_value.value_inr(lead)) == 750000.0


def test_a_lead_that_already_has_a_value_is_not_asked_again(world):
    r = _client(ADMIN).post(f'/api/leads/{world["priced"]}/advance',
                            json={'to_stage': 'RFQ Generated'})
    assert r.status_code == 200


def test_advancing_to_an_early_stage_needs_nothing(world):
    r = _client(ADMIN).post(f'/api/leads/{world["bare"]}/advance',
                            json={'to_stage': 'Profile Sent'})
    assert r.status_code == 200


# ── the lead form ────────────────────────────────────────────────────
def test_the_form_cannot_set_a_late_stage_without_a_value(world):
    r = _client(ADMIN).put(f'/api/leads/{world["bare"]}',
                           json={'stage': 'Quoted'})
    assert r.status_code == 400
    assert r.get_json()['needs'] == 'estimated_value'
    with flask_app.app_context():
        assert db.session.get(Lead, world['bare']).stage == 'Call Done'


def test_the_form_may_send_the_value_in_the_same_save(world):
    r = _client(ADMIN).put(f'/api/leads/{world["bare"]}',
                           json={'stage': 'Quoted',
                                 'estimated_value': '123456'})
    assert r.status_code == 200, r.get_json()
    with flask_app.app_context():
        from app.services import lead_value
        lead = db.session.get(Lead, world['bare'])
        assert lead.stage == 'Quoted'
        assert float(lead_value.value_inr(lead)) == 123456.0


def test_editing_a_late_stage_lead_without_touching_the_stage_is_fine(world):
    """The gate is about movement. A lead already at RFQ with no
    value must still be editable — otherwise the only way to fix it
    is blocked by the rule that wants it fixed."""
    r = _client(ADMIN).put(f'/api/leads/{world["past_rfq"]}',
                           json={'city': 'Chennai'})
    assert r.status_code == 200


# ── the decision dialog ──────────────────────────────────────────────
def test_won_needs_a_value(world):
    r = _client(ADMIN).post(f'/api/leads/{world["bare"]}/decision',
                            json={'outcome': 'Won'})
    assert r.status_code == 400
    assert r.get_json()['needs'] == 'estimated_value'


def test_won_with_a_value_goes_through(world):
    r = _client(ADMIN).post(f'/api/leads/{world["bare"]}/decision',
                            json={'outcome': 'Won', 'value': '900000'})
    assert r.status_code == 200, r.get_json()


def test_lost_needs_no_value(world):
    r = _client(ADMIN).post(f'/api/leads/{world["bare"]}/decision',
                            json={'outcome': 'Lost', 'reason': 'Price too high'})
    assert r.status_code == 200, r.get_json()
    with flask_app.app_context():
        assert db.session.get(Lead, world['bare']).stage == 'Lost'


# ── the bulk path ────────────────────────────────────────────────────
def test_bulk_skips_the_valueless_and_moves_the_rest(world):
    """A bulk action has nowhere to ask, so it skips with the reason.
    Skipping is right: the others in the selection should still go."""
    from app.workbench import bulk as wb_bulk
    with flask_app.app_context():
        sc = sc_mod.for_employee(ADMIN)
        changes, skipped, _stored = wb_bulk.plan(
            [world['bare'], world['priced']], 'set_stage',
            'RFQ Generated', sc)
    moved = {c['id'] for c in changes}
    stuck = {s['id']: s['reason'] for s in skipped}
    assert world['priced'] in moved
    assert world['bare'] in stuck
    assert 'estimated value' in stuck[world['bare']]


# ── the tile ─────────────────────────────────────────────────────────
def test_the_tile_counts_open_leads_past_rfq_with_no_value(world):
    with flask_app.app_context():
        d = wb.board(sc_mod.for_employee(ADMIN), filter_key='value_missing',
                     per_page=200)
    ids = {i['id'] for i in d['items']}
    assert world['past_rfq'] in ids
    assert world['quoted_ok'] not in ids, 'it has a value'
    assert world['bare'] not in ids, 'it is only at Call Done'


def test_the_tile_is_on_the_board_and_in_the_registry():
    f = wf.get('value_missing')
    assert f is not None
    assert f.tile == 'Value missing' and f.chip == 'Value missing'


def test_the_tile_count_matches_the_rows_behind_it(world):
    with flask_app.app_context():
        sc = sc_mod.for_employee(ADMIN)
        board = wb.board(sc, per_page=1)
        rows = wb.board(sc, filter_key='value_missing', per_page=200)
    assert board['counts']['value_missing'] == rows['total']


def test_the_report_population_is_the_same_rule(world):
    """The report and the tile must not be able to disagree."""
    with flask_app.app_context():
        sc = sc_mod.for_employee(ADMIN)
        from_service = {l.id for l in value_gate.missing_value_leads(sc)}
        from_board = {i['id'] for i in wb.board(
            sc, filter_key='value_missing', per_page=200)['items']}
    assert from_service == from_board


# ── the sentence people will read ────────────────────────────────────
def test_the_refusal_says_what_to_do(world):
    r = _client(ADMIN).post(f'/api/leads/{world["bare"]}/advance',
                            json={'to_stage': 'RFQ Generated'})
    msg = r.get_json()['error']
    assert 'estimated value' in msg.lower()
    assert 'corrected later' in msg, \
        'a refusal that does not say the estimate can be rough invites ' \
        'a made-up precise number'
