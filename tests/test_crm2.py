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


def _client(emp='CRM2A'):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['emp_code'] = emp
        s['role'] = 'admin' if emp == 'CRM2A' else 'sales'
    return c


@pytest.fixture
def armed():
    """The flag on, for this test only."""
    with flask_app.app_context():
        was = flags.on('FEATURE_CRM2')
        os.environ['FEATURE_CRM2'] = '1'
        flags.cache_clear()
        yield
        os.environ.pop('FEATURE_CRM2', None)
        flags.cache_clear()
        assert flags.on('FEATURE_CRM2') == was


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
