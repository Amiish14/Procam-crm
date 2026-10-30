"""
The open-stage sweep: alias stages count as open.

`sales_rules.open_stages()` was the journey's open stages and
nothing else, so a lead still carrying Profile Sent, Appointment or
Visit Done — the three names `stage_map.ALIASES` folds away, which
are only rewritten when somebody saves the lead — read as closed in
every screen, report and count built on that helper.

Two lists, kept apart on purpose, and the negative assertions are
the point:

    open_stages()      every stage a lead can be sitting on and
                       still be in play, aliases included
    pipeline_stages()  the vocabulary — a dropdown, a funnel's
                       order — which is the journey alone

The funnel test holds the other half: an alias lead counts, and it
counts on the row of the stage it means rather than on a row of its
own that the drawing loop never asks for.
"""
import importlib
import importlib.util
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'open-stage-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'OpenStageTest-123456')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'openstage.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
db = _main.db
Company, Lead = _main.Company, _main.Lead

from app.services import sales_rules, stage_map       # noqa: E402

PREFIX = 'OS '
ALIASES = ('Profile Sent', 'Appointment', 'Visit Done')


def _script():
    path = os.path.join(_ROOT, 'scripts', 'open_stage_sweep.py')
    spec = importlib.util.spec_from_file_location('openstagesweep', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sweep = _script()


def _purge():
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


def _lead(name, *, stage='New Opportunity', owner='OS_OWNER',
          account=None, value=1000000):
    lead = Lead(company=PREFIX + name, stage=stage, source='manual',
                assigned_to=owner, estimated_value_inr=value,
                company_id=account.id if account else None)
    db.session.add(lead)
    db.session.flush()
    return lead


def _account(name='Acme'):
    account = Company(name=PREFIX + name, is_active=True)
    db.session.add(account)
    db.session.flush()
    return account


# ── the two lists ────────────────────────────────────────────────────
def test_the_alias_stages_are_open():
    for alias in ALIASES:
        assert alias in sales_rules.open_stages()
        assert sales_rules.is_open(alias)


def test_the_vocabulary_is_still_the_journey_alone():
    """What a person may choose has not changed. An alias is a name
    nobody should be able to pick again."""
    assert list(sales_rules.pipeline_stages()) == [
        s for s in stage_map.JOURNEY if s not in stage_map.TERMINAL]
    for alias in ALIASES:
        assert alias not in sales_rules.pipeline_stages()
        assert alias not in _main.STAGES_PIPELINE


def test_open_is_the_journey_plus_the_open_aliases():
    assert set(sales_rules.open_stages()) == (
        set(sales_rules.pipeline_stages()) | set(ALIASES))
    assert _main.STAGES_OPEN == (
        _main.STAGES_PIPELINE + list(stage_map.OPEN_ALIASES))


def test_no_terminal_stage_is_open():
    for stage in stage_map.TERMINAL:
        assert stage not in sales_rules.open_stages()
        assert not sales_rules.is_open(stage)


def test_on_hold_stays_closed():
    """The 59 parked leads are still parked. Widening "open" to the
    aliases must not quietly reopen a terminal state."""
    assert 'On Hold' not in sales_rules.open_stages()


def test_open_aliases_leave_out_any_alias_that_folds_onto_a_terminal():
    for alias in stage_map.OPEN_ALIASES:
        assert stage_map.ALIASES[alias] not in stage_map.TERMINAL
    for alias, target in stage_map.ALIASES.items():
        if target in stage_map.TERMINAL:
            assert alias not in stage_map.OPEN_ALIASES


def test_canonical_folds_an_alias_and_leaves_a_journey_stage_alone():
    assert stage_map.canonical('Visit Done') == 'Business Discussion'
    assert stage_map.canonical(' Profile Sent ') == 'Call Done'
    assert stage_map.canonical('Quoted') == 'Quoted'
    assert stage_map.canonical(None) == ''
    assert sales_rules.canonical_stage('Appointment') == 'Call Done'


def test_every_open_stage_sits_somewhere_on_the_line():
    """A stage counted as open that the lead page cannot draw would
    be a lead in the figures and nowhere on the screen."""
    for stage in sales_rules.open_stages():
        assert stage_map.position(stage) is not None


# ── what the queries now see ─────────────────────────────────────────
def test_open_leads_picks_up_an_alias_lead():
    with flask_app.app_context():
        from app.access import scope as sc_mod
        kept = _lead('Alias', stage='Visit Done')
        closed = _lead('Closed', stage='Lost')
        db.session.commit()
        sc = sc_mod.Scope('OS_ADMIN', '', None, set(), None)
        ids = {l.id for l in sales_rules.open_leads(sc=sc).all()}
        assert kept.id in ids
        assert closed.id not in ids


def test_the_funnel_counts_an_alias_lead_on_the_stage_it_means():
    with flask_app.app_context():
        from app.review import service as review
        account = _account()
        _lead('Discussion', stage='Business Discussion', account=account)
        _lead('Visit', stage='Visit Done', account=account)
        db.session.commit()
        leads = Lead.query.filter(Lead.company.like(PREFIX + '%')).all()
        today = sales_rules.business_today()
        out = review._funnel(leads, today.replace(day=1), today)
        rows = {r['stage']: r['count'] for r in out['stages']}
        assert out['open_count'] == 2
        assert rows['Business Discussion'] == 2
        assert 'Visit Done' not in rows
        # The funnel draws the journey, not the stages it happened to
        # find, so the row order is stable from one week to the next.
        assert [r['stage'] for r in out['stages']] == \
            list(sales_rules.pipeline_stages())


def test_the_stage_vocabulary_endpoint_publishes_both_lists():
    client = flask_app.test_client()
    with flask_app.app_context():
        with client.session_transaction() as s:
            s['emp_code'] = 'PCM001'
            s['role'] = 'admin'
            s['must_change_pw'] = False
        data = client.get('/api/workflow/stages').get_json()
    assert data['pipeline'] == _main.STAGES_PIPELINE
    assert data['open'] == _main.STAGES_OPEN
    for alias in ALIASES:
        assert alias in data['open']
        assert alias not in data['pipeline']


# ── the sweep script ─────────────────────────────────────────────────
def test_the_sweep_knows_the_old_set_and_the_new_one():
    assert set(sweep.old_set()) == set(sales_rules.pipeline_stages())
    with flask_app.app_context():
        assert set(sweep.new_set()) == set(sales_rules.open_stages())


def test_every_site_the_sweep_lists_exists():
    """The list earns its keep only if the file and line are real."""
    for where, feeds, probe in sweep.SITES:
        path = where.split(':')[0]
        assert os.path.exists(os.path.join(_ROOT, path)), path
        assert feeds
        assert probe in sweep.PROBES


def test_the_sweep_counts_the_alias_leads_as_the_difference():
    with flask_app.app_context():
        account = _account()
        for i in range(3):
            _lead(f'Alias{i}', stage='Visit Done', account=account)
        _lead('Open', stage='Quoted', account=account)
        _lead('Closed', stage='Lost', account=account)
        db.session.commit()
        rows = {r['probe']: r for r in sweep.counts()}
        opened = rows['open']
        assert opened['after'] - opened['before'] == 3
        assert rows['value']['after'] - rows['value']['before'] == 3.0
        assert sweep.alias_rows().get('Visit Done') == 3


def test_the_sweep_leaves_the_data_alone():
    with flask_app.app_context():
        _lead('Untouched', stage='Visit Done')
        db.session.commit()
        before = [(l.id, l.stage, l.assigned_to) for l in Lead.query.filter(
            Lead.company.like(PREFIX + '%')).all()]
        sweep.report(sites=True)
        after = [(l.id, l.stage, l.assigned_to) for l in Lead.query.filter(
            Lead.company.like(PREFIX + '%')).all()]
        assert before == after
