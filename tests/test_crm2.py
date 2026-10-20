"""
CRM 2.0 R1 and R2: the cockpit, the search box and the stage map.

Two things here are worth more than the rest.

A global search box is the natural home of a scope leak. Five
per-tab filters exist today and each one is narrow by accident; one
box over every record type is narrow only if somebody wrote the
scoping, so `test_search_does_not_widen_scope` is the point of this
module rather than a detail in it.

And the stage map must say "unknown" where it does not know. The
obvious substitute for a missing stage date is `updated_at`, which
on this database is a lie — one bulk edit touched 2,381 leads in a
day — so a grey stage has to stay grey.
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'crm2-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Crm2Test-12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'crm2.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
db = _main.db
Company, Employee, Lead = _main.Company, _main.Employee, _main.Lead

from app.services import flags, stage_map                 # noqa: E402


# ── the line itself, no database ─────────────────────────────────────
class _L:
    def __init__(self, stage, entered=None, lid=1, value=0):
        self.id = lid
        self.stage = stage
        self.stage_entered_at = entered
        self.estimated_value_inr = value


def test_a_stage_with_no_date_is_grey_not_green():
    """The whole point. Green means "we checked and it is fine"."""
    m = stage_map.for_lead(_L('RFQ Generated', None))
    assert m['colour'] == stage_map.GREY
    assert m['known'] is False
    assert 'no stage date' in m['why']


@pytest.mark.parametrize('days,colour', [
    (0, stage_map.GREEN),
    (7, stage_map.GREEN),      # target for RFQ Generated is 7
    (8, stage_map.ORANGE),
    (14, stage_map.ORANGE),
    (15, stage_map.RED),
])
def test_the_colour_follows_the_target_for_that_stage(days, colour):
    when = datetime.utcnow() - timedelta(days=days)
    m = stage_map.for_lead(_L('RFQ Generated', when))
    assert m['colour'] == colour, m['why']


def test_the_target_is_per_stage_not_one_number():
    """A quote waiting a week is normal; a new lead waiting a week
    is not."""
    when = datetime.utcnow() - timedelta(days=6)
    assert stage_map.for_lead(_L('New', when))['colour'] \
        == stage_map.RED
    assert stage_map.for_lead(_L('Quoted', when))['colour'] \
        == stage_map.GREEN


def test_an_old_stage_still_draws_on_the_line():
    """3 leads sit on Profile Sent and 2 on Visit Done. Neither is
    in the journey, and neither should vanish from the map."""
    m = stage_map.for_lead(_L('Profile Sent', datetime.utcnow()))
    assert m['on_the_line']
    current = [s for s in m['steps'] if s['state'] == 'current']
    assert [s['stage'] for s in current] == ['Call Done']


def test_a_stage_nobody_mapped_is_not_guessed_at():
    m = stage_map.for_lead(_L('Some New Stage', datetime.utcnow()))
    assert m['on_the_line'] is False
    assert all(s['state'] == 'unknown' for s in m['steps'])


def test_a_closed_lead_shows_how_it_ended():
    m = stage_map.for_lead(_L('Lost', datetime.utcnow()))
    assert m['closed'] and not m['won']
    assert m['why'] == 'closed as Lost'


def test_won_is_the_end_of_the_line_not_a_closure():
    m = stage_map.for_lead(_L('Won', datetime.utcnow()))
    assert m['won'] and not m['closed']


def test_the_pipeline_leaves_out_what_already_stopped():
    """8,340 closed leads have no place on a map of stuck work."""
    rows = stage_map.pipeline([
        _L('New', datetime.utcnow(), 1),
        _L('Lost', datetime.utcnow(), 2),
        _L('Not Interested', datetime.utcnow(), 3),
        _L('Won', datetime.utcnow(), 4),
    ])
    assert sum(r['total'] for r in rows) == 1
    assert 'Won' not in [r['stage'] for r in rows]


def test_the_pipeline_counts_unknown_separately():
    rows = stage_map.pipeline([_L('New', None, 1),
                               _L('New', datetime.utcnow(), 2)])
    new = [r for r in rows if r['stage'] == 'New'][0]
    assert new['total'] == 2 and new['grey'] == 1 and new['green'] == 1


# ── the routes ───────────────────────────────────────────────────────
@pytest.fixture
def world():
    with flask_app.app_context():
        for l in Lead.query.filter(Lead.source == 'crm2t').all():
            db.session.delete(l)
        for c in Company.query.filter(
                Company.created_by == 'crm2t').all():
            db.session.delete(c)
        db.session.commit()

        for code, name, role in (('CRM2A', 'Crm2 Admin', 'admin'),
                                 ('CRM2S', 'Crm2 Seller', 'sales')):
            if not Employee.query.filter_by(emp_code=code).first():
                db.session.add(Employee(
                    emp_code=code, name=name, role=role,
                    email=f'{code.lower()}@example.com',
                    is_active=True))
        acct = Company(name='Zephyr Castings Limited', is_active=True,
                       created_by='crm2t', city='Pune')
        db.session.add(acct)
        db.session.flush()
        mine = Lead(source='crm2t', company='Zephyr Castings Limited',
                    company_id=acct.id, stage='RFQ Generated',
                    assigned_to='CRM2S', email='buyer@zephyr.example',
                    stage_entered_at=datetime.utcnow()
                    - timedelta(days=40))
        theirs = Lead(source='crm2t', company='Zephyr Rival Limited',
                      stage='New', assigned_to='CRM2A',
                      email='other@rival.example')
        db.session.add_all([mine, theirs])
        db.session.commit()
        yield {'mine': mine.id, 'theirs': theirs.id, 'acct': acct.id}


def _post(client, url, payload=None):
    """POST the way the page does, with a token.

    Not by turning CSRF off for this module: the Confirm button is a
    write, and a write without a token has silently done nothing six
    times in this codebase. If the token handling here is wrong, the
    button is wrong, and the test should say so.
    """
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    with flask_app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as s:
        s['csrf_token'] = raw
    return client.post(url, json=payload or {},
                       headers={'X-CSRFToken': token})


def _client(emp='CRM2A'):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['emp_code'] = emp
        s['role'] = 'admin' if emp == 'CRM2A' else 'sales'
    return c


@pytest.fixture
def armed():
    """The flag on, for this test only.

    The stored row is cleared first, not just the environment. The
    database beats the environment by design — that is the whole
    point of moving flags out of .env — so a test that turned the
    flag off through the admin path leaves a row that silently
    outranks `FEATURE_CRM2=1` here, and every test after it 404s.
    """
    with flask_app.app_context():
        flags.clear('FEATURE_CRM2', actor='TEST')
        flags.limit_to('FEATURE_CRM2', [], actor='TEST')
        os.environ['FEATURE_CRM2'] = '1'
        flags.cache_clear()
        yield
        os.environ.pop('FEATURE_CRM2', None)
        flags.clear('FEATURE_CRM2', actor='TEST')
        flags.limit_to('FEATURE_CRM2', [], actor='TEST')
        flags.cache_clear()
        assert not flags.on('FEATURE_CRM2')


@pytest.mark.parametrize('path', [
    '/crm2', '/api/crm2/cockpit', '/api/crm2/search?q=zephyr',
    '/api/crm2/pipeline', '/api/crm2/leads/1/stage-map',
])
def test_every_route_is_404_while_the_flag_is_off(path):
    """An unbuilt feature should look unbuilt from outside."""
    with flask_app.app_context():
        flags.cache_clear()
    assert _client().get(path).status_code == 404


def test_the_flag_can_be_set_from_the_database(armed):
    """FEATURE_FF_CRM was documented and never listed in FLAGS, so
    `_stored()` skipped its row and only the environment could turn
    it on. Both are listed now."""
    assert 'FEATURE_CRM2' in flags.NAMES
    assert 'FEATURE_FF_CRM' in flags.NAMES


def test_the_cockpit_answers_with_the_numbers_it_shows(armed, world):
    body = _client().get('/api/crm2/cockpit').get_json()
    assert body['ok']
    c = body['cockpit']
    for key in ('open_leads', 'value_missing', 'unowned', 'unlinked',
                'pipeline', 'stage_dates_missing'):
        assert key in c
    assert c['open_leads'] >= 2


def test_search_finds_a_lead_an_account_and_says_which(armed, world):
    body = _client().get('/api/crm2/search?q=Zephyr').get_json()
    kinds = {r['kind'] for r in body['results']}
    assert 'lead' in kinds and 'account' in kinds
    assert body['counts']['lead'] >= 1


def test_search_does_not_widen_scope(armed, world):
    """The reason this module exists.

    CRM2S owns one of the two Zephyr leads. One box over every record
    type must not hand them the other.
    """
    body = _client('CRM2S').get(
        '/api/crm2/search?q=Zephyr').get_json()
    ids = {r['id'] for r in body['results'] if r['kind'] == 'lead'}
    assert world['mine'] in ids
    assert world['theirs'] not in ids, \
        'the search returned a lead outside the viewer\'s scope'


def test_a_one_letter_search_is_refused_not_answered(armed, world):
    body = _client().get('/api/crm2/search?q=Z').get_json()
    assert body['results'] == []
    assert 'at least' in body['note']


def test_the_stage_map_route_is_scoped_too(armed, world):
    assert _client('CRM2S').get(
        f'/api/crm2/leads/{world["mine"]}/stage-map'
    ).status_code == 200
    assert _client('CRM2S').get(
        f'/api/crm2/leads/{world["theirs"]}/stage-map'
    ).status_code == 404


def test_the_stage_map_route_reports_the_age(armed, world):
    body = _client().get(
        f'/api/crm2/leads/{world["mine"]}/stage-map').get_json()
    assert body['map']['days_in_stage'] == 40
    assert body['map']['colour'] == stage_map.RED


def test_nothing_changes_on_the_existing_screens(world):
    """With the flag off the CRM behaves exactly as before — which is
    what makes the rollback a switch rather than a revert."""
    with flask_app.app_context():
        flags.cache_clear()
    c = _client()
    assert c.get('/my-work').status_code in (200, 302)
    assert c.get('/crm2').status_code == 404


# ── R2 migrations, run the way a person runs them ────────────────────
def _run(script, *flags):
    import subprocess
    with flask_app.app_context():
        url = str(db.engine.url)
    return subprocess.run(
        [sys.executable, os.path.join(_ROOT, 'scripts', script), *flags],
        capture_output=True, text=True, cwd=_ROOT,
        env={**os.environ, 'PYTHONPATH': _ROOT, 'DATABASE_URL': url})


@pytest.fixture
def closable():
    with flask_app.app_context():
        for l in Lead.query.filter(Lead.source == 'crm2m').all():
            db.session.delete(l)
        db.session.commit()
        bare = Lead(source='crm2m', company='Bare Co',
                    stage='Not Interested')
        spoken = Lead(source='crm2m', company='Spoken Co',
                      stage='Not Interested',
                      lost_reason='Chose the incumbent')
        db.session.add_all([bare, spoken])
        db.session.commit()
        yield {'bare': bare.id, 'spoken': spoken.id}


def test_the_merge_dry_run_writes_nothing(closable):
    out = _run('crm2_not_interested_to_lost.py', '--check')
    assert out.returncode == 0, out.stderr[-2000:]
    assert 'DRY RUN' in out.stdout
    with flask_app.app_context():
        assert db.session.get(
            Lead, closable['bare']).stage == 'Not Interested'


def test_the_merge_keeps_a_reason_somebody_typed(closable):
    out = _run('crm2_not_interested_to_lost.py', '--apply')
    assert out.returncode == 0, out.stderr[-3000:]
    with flask_app.app_context():
        bare = db.session.get(Lead, closable['bare'])
        spoken = db.session.get(Lead, closable['spoken'])
        assert bare.stage == 'Lost' and bare.lost_reason \
            == 'Not interested'
        assert spoken.stage == 'Lost'
        assert spoken.lost_reason == 'Chose the incumbent', \
            'a reason a person wrote says more than the stage did'


def test_the_merge_goes_back(closable):
    _run('crm2_not_interested_to_lost.py', '--apply')
    out = _run('crm2_not_interested_to_lost.py', '--down', '--yes')
    assert out.returncode == 0, out.stderr[-3000:]
    with flask_app.app_context():
        bare = db.session.get(Lead, closable['bare'])
        spoken = db.session.get(Lead, closable['spoken'])
        assert bare.stage == 'Not Interested'
        assert bare.lost_reason is None, 'the reason it added is gone'
        assert spoken.lost_reason == 'Chose the incumbent'


def test_down_needs_yes():
    out = _run('crm2_not_interested_to_lost.py', '--down')
    assert out.returncode != 0


def test_the_backfill_dates_a_lead_from_its_audit_trail():
    with flask_app.app_context():
        from app.models.audit import AuditEvent
        for l in Lead.query.filter(Lead.source == 'crm2b').all():
            db.session.delete(l)
        db.session.commit()
        lead = Lead(source='crm2b', company='Dateless Co',
                    stage='Quoted', stage_entered_at=None)
        db.session.add(lead)
        db.session.flush()
        moved = datetime.utcnow() - timedelta(days=30)
        db.session.add(AuditEvent(
            occurred_at=moved, actor='X', action='lead.stage_change',
            entity_type='lead', entity_id=str(lead.id),
            old_value={'stage': 'RFQ Generated'},
            new_value={'stage': 'Quoted'}))
        db.session.commit()
        lid = lead.id

    out = _run('crm2_backfill_stage_entered.py', '--apply')
    assert out.returncode == 0, out.stderr[-3000:]
    with flask_app.app_context():
        got = db.session.get(Lead, lid).stage_entered_at
        assert got is not None
        assert abs((got - moved).total_seconds()) < 2

    out = _run('crm2_backfill_stage_entered.py', '--down', '--yes')
    assert out.returncode == 0
    with flask_app.app_context():
        assert db.session.get(Lead, lid).stage_entered_at is None


def test_the_backfill_refuses_a_stale_audit_row():
    """The newest change says it moved to Quoted; the lead says
    Won. Something moved it without an audit row, so we cannot date
    it — and an older row would be the wrong arrival."""
    with flask_app.app_context():
        from app.models.audit import AuditEvent
        lead = Lead(source='crm2b', company='Drifted Co', stage='Won',
                    stage_entered_at=None)
        db.session.add(lead)
        db.session.flush()
        db.session.add(AuditEvent(
            occurred_at=datetime.utcnow() - timedelta(days=9),
            actor='X', action='lead.stage_change', entity_type='lead',
            entity_id=str(lead.id), old_value={'stage': 'RFQ Generated'},
            new_value={'stage': 'Quoted'}))
        db.session.commit()
        lid = lead.id

    _run('crm2_backfill_stage_entered.py', '--apply')
    with flask_app.app_context():
        assert db.session.get(Lead, lid).stage_entered_at is None


def test_the_overlap_report_has_no_apply():
    out = _run('crm2_opportunity_overlap.py', '--check')
    assert out.returncode == 0, out.stderr[-2000:]
    assert 'READ ONLY' in out.stdout
    assert 'opportunities' in out.stdout


def test_the_overlap_report_joins_on_opp_number_too():
    """The first version joined on `lead_id` alone and called 9,522
    of 9,540 rows orphan history. `lead_id` is null on almost every
    row; `opp_number` matches a lead 9,524 times. A bucket that
    depends on the join has to try both keys."""
    with flask_app.app_context():
        Opportunity = _main.Opportunity
        for o in Opportunity.query.filter(
                Opportunity.opp_number.like('CRM2T-%')).all():
            db.session.delete(o)
        for l in Lead.query.filter(Lead.source == 'crm2o').all():
            db.session.delete(l)
        db.session.commit()

        lead = Lead(source='crm2o', company='Numbered Co', stage='Quoted',
                    opp_number='CRM2T-0001')
        db.session.add(lead)
        db.session.flush()
        # No lead_id at all — the shape 9,522 production rows have.
        db.session.add(Opportunity(opp_number='CRM2T-0001',
                                   title='Numbered', stage='Qualified',
                                   lead_id=None, value_inr=250000))
        db.session.commit()
        lid = lead.id

        import importlib.util as _u
        spec = _u.spec_from_file_location(
            'overlap', os.path.join(_ROOT, 'scripts',
                                    'crm2_opportunity_overlap.py'))
        mod = _u.module_from_spec(spec)
        spec.loader.exec_module(mod)
        s = mod.survey()

        reached = {l.id for _o, l, _k in s['matched']}
        assert lid in reached, 'matched only by opp_number, and missed'
        assert s['by_key']['opp_number'] >= 1


# ── 2: the flag, limited to the people reviewing it ──────────────────
def test_a_limited_flag_is_on_for_them_and_absent_for_everyone_else(
        armed, world):
    with flask_app.app_context():
        flags.limit_to('FEATURE_CRM2', ['CRM2A'], actor='TEST')
        try:
            assert flags.on_for('FEATURE_CRM2', 'CRM2A')
            assert not flags.on_for('FEATURE_CRM2', 'CRM2S')
            assert flags.only_for('FEATURE_CRM2') == ('CRM2A',)
            assert _client('CRM2A').get('/api/crm2/cockpit'
                                        ).status_code == 200
            assert _client('CRM2S').get('/api/crm2/cockpit'
                                        ).status_code == 404
        finally:
            flags.limit_to('FEATURE_CRM2', [], actor='TEST')
        assert flags.on_for('FEATURE_CRM2', 'CRM2S'), 'limit not lifted'


def test_a_limit_narrows_and_never_grants():
    """A reviewer list left behind on a flag somebody later turned
    off must not switch it back on for them."""
    with flask_app.app_context():
        flags.cache_clear()
        flags.limit_to('FEATURE_CRM2', ['CRM2A'], actor='TEST')
        try:
            assert not flags.on('FEATURE_CRM2')
            assert not flags.on_for('FEATURE_CRM2', 'CRM2A')
        finally:
            flags.limit_to('FEATURE_CRM2', [], actor='TEST')


def test_all_flags_still_unpacks_as_four():
    """Three call sites and a template unpack this."""
    for row in flags.all_flags():
        assert len(row) == 4


# ── 1: a copied value keeps asking ───────────────────────────────────
@pytest.fixture
def copied():
    with flask_app.app_context():
        for l in Lead.query.filter(Lead.source == 'crm2v').all():
            db.session.delete(l)
        db.session.commit()
        lead = Lead(source='crm2v', company='Copied Value Co',
                    stage='RFQ Generated', assigned_to='CRM2S',
                    estimated_value_inr=250000,
                    value_source='opportunity', value_confirmed=False)
        typed = Lead(source='crm2v', company='Typed Value Co',
                     stage='RFQ Generated', assigned_to='CRM2S',
                     estimated_value_inr=400000)
        db.session.add_all([lead, typed])
        db.session.commit()
        yield {'copied': lead.id, 'typed': typed.id}


def test_an_unconfirmed_value_stays_on_the_tile(copied):
    from app.workbench import filters as wbf
    item = {'kind': 'lead', 'stage': 'RFQ Generated',
            'value_inr': 250000.0, 'value_confirmed': False}
    assert wbf.BY_KEY['value_missing'].predicate(item, {})
    assert wbf.needs_confirming(item)


def test_a_value_somebody_typed_does_not(copied):
    from app.workbench import filters as wbf
    item = {'kind': 'lead', 'stage': 'RFQ Generated',
            'value_inr': 400000.0, 'value_confirmed': None}
    assert not wbf.BY_KEY['value_missing'].predicate(item, {})
    assert not wbf.needs_confirming(item)


def test_no_value_at_all_is_still_the_main_case():
    from app.workbench import filters as wbf
    item = {'kind': 'lead', 'stage': 'Quoted', 'value_inr': None,
            'value_confirmed': None}
    assert wbf.BY_KEY['value_missing'].predicate(item, {})
    assert not wbf.needs_confirming(item), 'nothing to confirm'


def test_an_unconfirmed_value_still_lets_the_lead_move():
    """It is a number. The gate asked for a number."""
    from app.services import value_gate

    class _L:
        stage = 'RFQ Generated'
        estimated_value_inr = 250000
        quoted_amount_inr = None
        value_confirmed = False
    assert value_gate.check(_L(), 'Quoted', {}) in (None, True, '') \
        or not value_gate.check(_L(), 'Quoted', {})


def test_confirming_takes_it_off_the_tile(armed, copied):
    c = _client('CRM2S')
    r = _post(c, f'/api/crm2/leads/{copied["copied"]}'
                 f'/confirm-value')
    assert r.status_code == 200, r.get_data(as_text=True)[:400]
    with flask_app.app_context():
        lead = db.session.get(Lead, copied['copied'])
        assert lead.value_confirmed is True
        assert lead.value_source == 'opportunity', \
            'confirming does not change where it came from'
        from app.models.audit import AuditEvent
        assert AuditEvent.query.filter_by(
            action='lead.value_confirmed',
            entity_id=str(lead.id)).count() == 1


def test_confirming_with_a_better_figure_records_the_owner(armed,
                                                           copied):
    c = _client('CRM2S')
    r = _post(c, f'/api/crm2/leads/{copied["copied"]}'
                 f'/confirm-value', {'estimated_value': 900000})
    assert r.status_code == 200
    with flask_app.app_context():
        lead = db.session.get(Lead, copied['copied'])
        assert float(lead.estimated_value_inr) == 900000
        assert lead.value_source == 'owner'


def test_confirming_nothing_is_refused(armed, world):
    c = _client()
    r = _post(c, f'/api/crm2/leads/{world["theirs"]}'
                 f'/confirm-value')
    assert r.status_code == 400
    assert 'no figure' in r.get_json()['error']


def test_confirming_is_scoped(armed, copied):
    with flask_app.app_context():
        other = Lead(source='crm2v', company='Not Yours Co',
                     stage='RFQ Generated', assigned_to='CRM2A',
                     estimated_value_inr=1, value_confirmed=False)
        db.session.add(other)
        db.session.commit()
        oid = other.id
    assert _post(_client('CRM2S'),
                 f'/api/crm2/leads/{oid}/confirm-value'
                 ).status_code == 404


# ── 3: R3 reads the engines, it does not become one ──────────────────
def test_the_cockpit_imports_no_engine_of_its_own():
    """The last second opinion about tasks created a twin row per
    task and doubled every count. This one asks."""
    src = open(os.path.join(_ROOT, 'app', 'services',
                            'cockpit.py')).read()
    for banned in ('TaskDefinition(', 'db.session.add(',
                   'db.session.commit('):
        assert banned not in src, \
            f'cockpit.py should read, not write or define: {banned}'
    assert 'lead_items' in src and 'TaskInstance' in src


def test_my_work_answers_the_three_questions(armed, world):
    body = _client('CRM2S').get('/api/crm2/my-work').get_json()
    assert body['ok']
    w = body['work']
    for key in ('next_action', 'overdue_leads', 'overdue_tasks',
                'escalated', 'to_confirm'):
        assert key in w
        assert key + '_total' in w


def test_my_work_is_one_persons_book_not_the_whole_one(armed, world):
    """An admin's scope is everything; a cockpit of 945 leads is a
    report, not a cockpit."""
    from app.services import cockpit
    from app.access import scope as sc_mod
    with flask_app.app_context():
        wide = sc_mod.for_employee('CRM2A')
        narrow = cockpit.mine_only(wide, 'CRM2S')
        assert narrow.codes == {'CRM2S'}
        assert wide.codes is None or wide.codes != {'CRM2S'}


def test_an_escalated_task_reaches_the_person_it_went_to(armed, world):
    from app.models.task_engine import TaskInstance
    with flask_app.app_context():
        t = TaskInstance(
            task_key='crm2.test', entity_type='lead',
            entity_id=world['mine'], entity_display='Escalated thing',
            owner_user_id='CRM2S', status='Escalated',
            escalated=True, escalated_to='admin.triage',
            escalated_at=datetime.utcnow())
        db.session.add(t)
        db.session.commit()
        tid = t.id
    body = _client('CRM2S').get('/api/crm2/my-work').get_json()
    assert tid in [r['id'] for r in body['work']['escalated']]
    with flask_app.app_context():
        db.session.delete(db.session.get(TaskInstance, tid))
        db.session.commit()


def test_the_limit_script_turns_it_on_for_one_person():
    out = _run('crm2_flag_limit.py', '--on', '--only', 'DIR12010',
               '--actor', 'TEST')
    assert out.returncode == 0, out.stderr[-2000:]
    assert 'visible to: DIR12010' in out.stdout
    try:
        with flask_app.app_context():
            flags.cache_clear()
            assert flags.on_for('FEATURE_CRM2', 'DIR12010')
            assert not flags.on_for('FEATURE_CRM2', 'SOMEONE')
    finally:
        # clear(), not --off: --off writes a `false` row, and a
        # stored row outranks the environment for every test after
        # this one.
        with flask_app.app_context():
            flags.limit_to('FEATURE_CRM2', [], actor='TEST')
            flags.clear('FEATURE_CRM2', actor='TEST')
            flags.cache_clear()
    assert not flags.on('FEATURE_CRM2')
