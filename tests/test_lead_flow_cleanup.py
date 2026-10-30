"""
CRM-LEAD-FLOW-01 §6 — the one-time clean-up.

Six read-only reports, each with counts and a sample. The apply
paths are tested too, not only the refusals: a gated `--apply` that
was never built reports a clean dry run and writes nothing.

Nothing deletes. A stale lead closes as Lost with a reason, a
blocked client's lead closes as Lost "Client blocked", a duplicate
is merged and its row kept pointing at the survivor.
"""
import importlib
import importlib.util
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'cleanup-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'CleanupTest-123456')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'cleanup.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
db = _main.db
Company, Lead = _main.Company, _main.Lead

PREFIX = 'CU '
ACTOR = 'CU_ACTOR'


def _script():
    path = os.path.join(_ROOT, 'scripts', 'lead_flow_cleanup.py')
    spec = importlib.util.spec_from_file_location('leadflowcleanup',
                                                  path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cu = _script()


def _purge():
    from app.models.audit import AuditEvent
    ids = [r.id for r in Lead.query.filter(
        Lead.company.like(PREFIX + '%')).all()]
    if ids:
        AuditEvent.query.filter(
            AuditEvent.entity_type == 'lead',
            AuditEvent.entity_id.in_([str(i) for i in ids])).delete(
                synchronize_session=False)
    Lead.query.filter(Lead.company.like(PREFIX + '%')).delete(
        synchronize_session=False)
    Company.query.filter(Company.name.like(PREFIX + '%')).delete(
        synchronize_session=False)
    db.session.commit()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        yield
        _purge()


def _lead(name='Acme', *, stage='New Opportunity', days=0,
          touched=None, account=None, industry='', state='',
          subject='', thread='', email='rfq@acme-test.com'):
    made = datetime.utcnow() - timedelta(days=days)
    lead = Lead(company=PREFIX + name, stage=stage, source='email',
                email=email, created_at=made,
                updated_at=made if touched is None
                else datetime.utcnow() - timedelta(days=touched),
                industry=industry, state=state,
                original_email_subject=subject, thread_key=thread,
                company_id=account.id if account else None)
    db.session.add(lead)
    db.session.flush()
    return lead


def _account(name='Acme', *, industry='', state=''):
    account = Company(name=PREFIX + name, is_active=True,
                      industry=industry, state=state)
    db.session.add(account)
    db.session.flush()
    return account


def _ours(rows, key='company'):
    return [r for r in rows if str(r.get(key, '')).startswith(PREFIX)]


# ── stages (§2.5, §6.6) ──────────────────────────────────────────────
def test_the_stage_report_counts_both_moves():
    with flask_app.app_context():
        _lead('One', stage='New Opportunity')
        _lead('Two', stage='Call Done')
        _lead('Three', stage='Visit Done')
        db.session.commit()
        data = cu.stages()
        assert data['fresh'] >= 1
        assert data['detailed']['Call Done'] >= 1
        assert 'Visit Done' in data['detailed']


def test_the_stage_report_changes_nothing():
    with flask_app.app_context():
        lead = _lead('One', stage='New Opportunity')
        db.session.commit()
        cu.stages()
        assert db.session.get(Lead, lead.id).stage == 'New Opportunity'


def test_applying_stages_renames_the_fresh_one():
    with flask_app.app_context():
        lead = _lead('One', stage='New Opportunity')
        db.session.commit()
        cu.apply_stages(actor=ACTOR)
        assert db.session.get(Lead, lead.id).stage == 'New'


def test_applying_stages_moves_the_detail_to_a_sub_status():
    """Reports that count "Visit Done" keep working, because the
    value is still on the row — just in the column that describes
    it."""
    with flask_app.app_context():
        lead = _lead('Two', stage='Visit Done')
        db.session.commit()
        cu.apply_stages(actor=ACTOR)
        row = db.session.get(Lead, lead.id)
        assert row.stage == 'Contacted'
        assert row.sub_status == 'Visit done'


def test_applying_stages_writes_an_audit_row():
    with flask_app.app_context():
        from app.models.audit import AuditEvent
        lead = _lead('One', stage='New Opportunity')
        db.session.commit()
        cu.apply_stages(actor=ACTOR)
        event = AuditEvent.query.filter_by(
            action='lead.stage_renamed',
            entity_id=str(lead.id)).first()
        assert event is not None and event.actor == ACTOR


# ── industry and state (§2.5, §6.7) ──────────────────────────────────
def test_the_industry_report_only_names_what_it_can_fill():
    with flask_app.app_context():
        rich = _account('Filled', industry='Power', state='Gujarat')
        bare = _account('Bare')
        wanted = _lead('From Filled', account=rich)
        _lead('From Bare', account=bare)
        _lead('No account')
        db.session.commit()
        rows = _ours(cu.industry()['rows'])
        assert [r['lead_id'] for r in rows] == [wanted.id]
        assert rows[0]['fill'] == {'industry': 'Power',
                                   'state': 'Gujarat'}


def test_a_lead_that_already_has_them_is_left_alone():
    with flask_app.app_context():
        rich = _account('Filled', industry='Power', state='Gujarat')
        _lead('Already', account=rich, industry='Steel',
              state='Odisha')
        db.session.commit()
        assert _ours(cu.industry()['rows']) == []


def test_applying_industry_fills_from_the_account():
    with flask_app.app_context():
        rich = _account('Filled', industry='Power', state='Gujarat')
        lead = _lead('From Filled', account=rich)
        db.session.commit()
        cu.apply_industry(actor=ACTOR)
        row = db.session.get(Lead, lead.id)
        assert row.industry == 'Power' and row.state == 'Gujarat'


# ── stale (§6.8) ─────────────────────────────────────────────────────
def test_the_stale_report_needs_both_the_lead_and_the_touch_to_be_old():
    with flask_app.app_context():
        old = _lead('Old', days=200, touched=200)
        _lead('Old but worked', days=200, touched=2)
        _lead('New', days=3, touched=3)
        db.session.commit()
        rows = _ours(cu.stale()['rows'])
        assert [r['lead_id'] for r in rows] == [old.id]


def test_applying_stale_closes_as_lost_with_a_reason():
    with flask_app.app_context():
        lead = _lead('Old', days=200, touched=200)
        db.session.commit()
        assert cu.apply_stale(actor=ACTOR) >= 1
        row = db.session.get(Lead, lead.id)
        assert row.stage == 'Lost'
        assert row.lost_reason == cu.STALE_REASON


def test_nothing_in_the_clean_up_deletes_a_lead():
    with flask_app.app_context():
        lead = _lead('Old', days=200, touched=200)
        db.session.commit()
        lead_id = lead.id
        cu.apply_stale(actor=ACTOR)
        assert db.session.get(Lead, lead_id) is not None


# ── duplicates (§6.1) ────────────────────────────────────────────────
def test_the_duplicate_report_groups_a_split_thread():
    with flask_app.app_context():
        account = _account('Indo Tech')
        for _ in range(3):
            _lead('Indo Tech', account=account, thread='c:ZZ',
                  subject='40 MVA transformer')
        db.session.commit()
        data = cu.duplicates()
        mine = [g for g in data['groups']
                if g['company'].startswith(PREFIX)]
        assert len(mine) == 1 and len(mine[0]['members']) == 3


def test_applying_duplicates_merges_only_the_certain_ones():
    """A subject-only match draws a banner on the screen. It is
    never merged by a sweep."""
    with flask_app.app_context():
        account = _account('Indo Tech')
        a = _lead('Indo Tech', account=account, thread='c:ZZ',
                  subject='40 MVA transformer')
        b = _lead('Indo Tech', account=account, thread='c:ZZ',
                  subject='RE: 40 MVA transformer')
        db.session.commit()
        done, _refused = cu.apply_duplicates(actor=ACTOR)
        assert done == 1
        survivors = [x for x in (a, b)
                     if db.session.get(Lead, x.id).merged_into_id
                     is None]
        assert len(survivors) == 1


# ── blocked clients (§6.3) ───────────────────────────────────────────
def test_the_blocked_report_is_empty_when_nothing_is_blocked():
    with flask_app.app_context():
        _lead('Ordinary')
        db.session.commit()
        assert _ours(cu.blocked()['rows']) == []


# ── the command line ─────────────────────────────────────────────────
def _run(argv):
    import contextlib
    import io
    out = io.StringIO()
    old = sys.argv
    sys.argv = ['lead_flow_cleanup.py'] + argv
    try:
        with contextlib.redirect_stdout(out):
            code = cu.main()
    finally:
        sys.argv = old
    return code, out.getvalue()


def test_the_command_line_is_read_only_by_default():
    with flask_app.app_context():
        lead = _lead('One', stage='New Opportunity')
        db.session.commit()
        lead_id = lead.id
    code, text = _run([])
    assert code == 0
    assert 'Nothing was changed' in text
    for name in cu.REPORTS:
        assert f'=== {name} ===' in text
    with flask_app.app_context():
        assert db.session.get(Lead, lead_id).stage == 'New Opportunity'


def test_apply_without_an_actor_writes_nothing():
    with flask_app.app_context():
        lead = _lead('One', stage='New Opportunity')
        db.session.commit()
        lead_id = lead.id
    code, text = _run(['--only', 'stages', '--apply'])
    assert code == 2
    assert 'Nothing was changed' in text
    with flask_app.app_context():
        assert db.session.get(Lead, lead_id).stage == 'New Opportunity'


def test_apply_without_a_section_writes_nothing():
    code, text = _run(['--actor', ACTOR, '--apply'])
    assert code == 2


def test_one_report_can_be_asked_for_on_its_own():
    code, text = _run(['--only', 'stages'])
    assert code == 0
    assert '=== stages ===' in text
    assert '=== stale ===' not in text


def test_the_command_line_applies_when_fully_armed():
    with flask_app.app_context():
        lead = _lead('One', stage='New Opportunity')
        db.session.commit()
        lead_id = lead.id
    code, _text = _run(['--only', 'stages', '--actor', ACTOR,
                        '--apply'])
    assert code == 0
    with flask_app.app_context():
        assert db.session.get(Lead, lead_id).stage == 'New'


def test_a_report_with_no_apply_path_says_so():
    code, text = _run(['--only', 'owners', '--actor', ACTOR,
                       '--apply'])
    assert code == 2
    assert 'no apply path here' in text
