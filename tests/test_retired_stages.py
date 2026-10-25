"""
A stage you removed from one dropdown is not a stage you removed.

R2 converted 1,212 leads off "Not Interested" and left the option
on the Decide dialog. Three more leads were filed under it within
three days — by people, after the conversion. The data was fixed;
the cause was not.

So these tests close every door rather than the one that was
noticed: the Decide dialog, the lead form's stage field, and the
bulk stage action. And they check the other half, which is the one
easy to forget — the replacement has to exist. Removing an outcome
without providing the reason that replaces it leaves people with
something they can see in the data and cannot record, and what
they do then is pick a different reason that is not true.
"""
import importlib
import os
import sys
import tempfile
from datetime import datetime

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'retired-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Retired-123456')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'retired.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
flask_app.config['WTF_CSRF_ENABLED'] = False
db = _main.db
Lead, Employee = _main.Lead, _main.Employee

from app.services import stage_map                        # noqa: E402

RETIRED = 'Not Interested'
ADMIN = 'EMP_RET_ADM'


def _purge():
    Lead.query.filter(Lead.company.like('RT%')).delete(
        synchronize_session=False)
    Employee.query.filter_by(emp_code=ADMIN).delete(
        synchronize_session=False)
    db.session.commit()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        db.session.add(Employee(
            emp_code=ADMIN, name='Retire Admin', password_hash='x',
            email='ret@procamlogistics.com', role='admin'))
        db.session.commit()
        yield
        _purge()


def _lead(stage='New'):
    lead = Lead(company='RT Co', stage=stage, source='manual',
                assigned_to=ADMIN, estimated_value_inr=100000,
                created_at=datetime.utcnow())
    db.session.add(lead)
    db.session.commit()
    return lead


def _client():
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['emp_code'] = ADMIN
        s['role'] = 'admin'
    return c


# ── it is gone from the choices ──────────────────────────────────────
def test_it_is_not_an_outcome_anybody_can_choose():
    assert RETIRED not in _main.DECISION_OUTCOMES
    assert _main.DECISION_OUTCOMES == ['Won', 'Lost', 'On Hold']


def test_it_is_still_terminal_for_everything_already_on_it():
    """The leads that carried it are genuinely closed, and every
    report that counts closed work must keep counting them."""
    assert RETIRED in stage_map.TERMINAL
    assert RETIRED in _main.STAGES_TERMINAL


def test_the_rule_says_what_to_do_instead():
    message = stage_map.retired(RETIRED)
    assert message and 'Lost' in message and 'Not interested' in message


def test_a_live_stage_is_not_retired():
    for stage in ('Won', 'Lost', 'On Hold', 'New', 'Quoted'):
        assert stage_map.retired(stage) is None


# ── every door, not the one that was noticed ─────────────────────────
def test_the_decide_dialog_refuses_it():
    with flask_app.app_context():
        lead_id = _lead().id
    r = _client().post(f'/api/leads/{lead_id}/decision',
                       json={'outcome': RETIRED, 'reason': 'x'})
    assert r.status_code == 400
    assert 'no longer a stage' in r.get_json()['error']


def test_the_lead_form_refuses_it():
    with flask_app.app_context():
        lead_id = _lead().id
    r = _client().put(f'/api/leads/{lead_id}',
                      json={'stage': RETIRED})
    assert r.status_code == 400
    assert 'no longer a stage' in r.get_json()['error']


def test_the_bulk_action_skips_it_with_the_reason():
    from app.access import scope as sc_mod
    from app.workbench import bulk

    with flask_app.app_context():
        lead = _lead()
        # `codes=None` means no restriction — the widest scope,
        # so the lead is certainly visible and the only thing that
        # can stop the change is the rule under test.
        scope = sc_mod.Scope(emp_code=ADMIN, vertical='', codes=None,
                             perms=(), data_scope='all')
        changes, skipped, _stored = bulk.plan(
            [lead.id], 'set_stage', RETIRED, scope)
    assert changes == []
    assert 'no longer a stage' in skipped[0]['reason']


def test_a_lead_cannot_reach_it_by_stepping_forward():
    """STAGE_NEXT is derived from the journey; nothing steps here."""
    assert RETIRED not in _main.STAGE_NEXT.values()


def test_closing_as_lost_still_works():
    """The replacement path has to be open, or people pick something
    untrue instead."""
    with flask_app.app_context():
        lead_id = _lead().id
    r = _client().post(f'/api/leads/{lead_id}/decision',
                       json={'outcome': 'Lost',
                             'reason': 'Not interested'})
    assert r.status_code == 200, r.get_data(as_text=True)
    with flask_app.app_context():
        lead = db.session.get(Lead, lead_id)
        assert lead.stage == 'Lost'
        assert lead.lost_reason == 'Not interested'
