"""
What the pilot still needs, said before a customer finds out.

CRM-FINISH-ALL.

Six releases are built and most of what remains is not code: a
coordinator nobody has named, an SCM table with a gap, terms
still marked DRAFT, a mail switch nobody has turned on. Every one
of those fails silently — a step opens owned by nobody, a
clarification is composed and never sent.

The two properties worth guarding:

    it is read-only, so it can be run against production at any
    time without anybody thinking twice;

    it reports only on verticals somebody is actually piloting. A
    vertical nobody has switched on has nothing to be ready for,
    and listing it as MISSING teaches people to ignore the list.
"""
import importlib
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'ready-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Readiness-12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'ready.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
importlib.import_module('app.models.rfq_target')
importlib.import_module('app.models.quote_terms')
flask_app = _main.app
flask_app.config['WTF_CSRF_ENABLED'] = False
db = _main.db
Employee = _main.Employee

from app.models.quote_terms import QuoteTermSet            # noqa: E402
from app.models.rfq_target import RfqTarget                # noqa: E402
from app.services import pilot_readiness as pr             # noqa: E402

PILOT, ADMIN = 'PR_PILOT', 'PR_ADMIN'
VERTICAL = 'Warehousing'


def _purge():
    from app.models.integration import AppSetting
    RfqTarget.query.filter(RfqTarget.emp_code.like('PR_%')).delete(
        synchronize_session=False)
    QuoteTermSet.query.filter(
        QuoteTermSet.title == 'PR Terms').delete(
            synchronize_session=False)
    Employee.query.filter(Employee.emp_code.like('PR_%')).delete(
        synchronize_session=False)
    AppSetting.query.filter(
        AppSetting.key.in_(('rfq_coordinator', 'rfq_scm_by_vertical',
                            'rfq_ops_by_vertical'))).delete(
            synchronize_session=False)
    AppSetting.query.filter(
        AppSetting.key.like('flag.FEATURE_RFQ_CHAIN%')).delete(
            synchronize_session=False)
    db.session.commit()
    from app.services import flags
    flags.cache_clear()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        db.session.add(Employee(
            emp_code=PILOT, name='PR Pilot', password_hash='x',
            role='user', vertical=VERTICAL,
            email='prp@procamlogistics.com'))
        db.session.add(Employee(
            emp_code=ADMIN, name='PR Admin', password_hash='x',
            role='admin', vertical='All',
            email='pra@procamlogistics.com'))
        db.session.commit()
        yield
        _purge()


def _chain_on(*codes):
    from app.services import flags
    import json
    from app.models.integration import AppSetting
    row = AppSetting.query.filter_by(
        key='flag.FEATURE_RFQ_CHAIN').first()
    if row is None:
        row = AppSetting(key='flag.FEATURE_RFQ_CHAIN')
        db.session.add(row)
    row.value_json = json.dumps(True)
    db.session.commit()
    flags.limit_to('FEATURE_RFQ_CHAIN', list(codes), actor='test')
    flags.cache_clear()


# ── who it reports on ───────────────────────────────────────────────
def test_nothing_is_reported_before_anybody_is_piloting():
    """A vertical nobody switched on has nothing to be ready for,
    and listing it teaches people to ignore the list."""
    with flask_app.app_context():
        data = pr.report()
        assert data['no_pilot'] is True
        assert data['verticals'] == {}


def test_only_the_verticals_actually_switched_on_appear():
    with flask_app.app_context():
        _chain_on(PILOT)
        data = pr.report()
        assert list(data['verticals']) == [VERTICAL]
        assert data['people'][VERTICAL] == [PILOT]


def test_somebody_who_spans_everything_names_no_vertical():
    """An "All" person tells us nothing about which vertical is
    being piloted."""
    with flask_app.app_context():
        _chain_on(ADMIN)
        data = pr.report()
        assert data['verticals'] == {}


# ── the checks ──────────────────────────────────────────────────────
def _by_key(rows):
    return {c['key']: c for c in rows}


def test_a_missing_coordinator_is_missing_and_names_who_to_ask():
    with flask_app.app_context():
        _chain_on(PILOT)
        check = _by_key(pr.report()['company'])['rfq_coordinator']
        assert check['state'] == pr.MISSING
        assert 'owned by nobody' in check['detail']
        assert check['who'] and check['fix']


def test_a_set_coordinator_is_ok():
    from app.services import admin_settings

    with flask_app.app_context():
        _chain_on(PILOT)
        admin_settings.save('rfq_coordinator', PILOT, actor='test')
        db.session.commit()
        assert _by_key(pr.report()['company'])[
            'rfq_coordinator']['state'] == pr.OK


def test_a_gap_in_the_scm_table_is_found():
    from app.services import admin_settings

    with flask_app.app_context():
        _chain_on(PILOT)
        admin_settings.save('rfq_scm_by_vertical',
                            {'Heavy Transport': PILOT}, actor='test')
        db.session.commit()
        rows = _by_key(pr.report()['verticals'][VERTICAL])
        assert rows['rfq_scm_by_vertical']['state'] == pr.MISSING
        assert VERTICAL in rows['rfq_scm_by_vertical']['detail']


def test_external_mail_being_off_is_reported():
    with flask_app.app_context():
        _chain_on(PILOT)
        check = _by_key(pr.report()['company'])[
            'CRM_EMAIL_ALLOW_EXTERNAL']
        assert check['state'] == pr.MISSING
        assert 'never sent' in check['detail']


def test_draft_terms_are_reported_with_the_verticals_named():
    with flask_app.app_context():
        _chain_on(PILOT)
        db.session.add(QuoteTermSet(
            vertical=VERTICAL, version=99, title='PR Terms',
            body='DRAFT — replace before issuing.\n1. Something.',
            is_current=True))
        db.session.commit()
        check = _by_key(pr.report()['company'])['quote_term_sets']
        assert check['state'] == pr.MISSING
        assert VERTICAL in check['detail']
        assert check['who'] == 'the commercial team'


def test_missing_targets_name_the_person_without_one():
    with flask_app.app_context():
        _chain_on(PILOT)
        from app.services import lead_journey as lj
        db.session.add(RfqTarget(month=lj.month_key(),
                                 vertical=VERTICAL,
                                 emp_code='PR_OTHER', target_rfqs=4))
        db.session.commit()
        check = _by_key(pr.report()['verticals'][VERTICAL])[
            'rfq_targets']
        assert check['state'] == pr.MISSING
        assert PILOT in check['detail']


def test_the_sender_block_being_off_is_a_warning_not_a_blocker():
    """It is a quality problem, not something that stops a pilot."""
    with flask_app.app_context():
        _chain_on(PILOT)
        check = _by_key(pr.report()['company'])[
            'FEATURE_SENDER_BLOCK']
        assert check['state'] in (pr.OK, pr.WARN)
        if check['state'] == pr.WARN:
            assert 'leads again' in check['detail']


def test_the_summary_counts_what_is_missing():
    with flask_app.app_context():
        _chain_on(PILOT)
        data = pr.report()
        assert data['missing'] > 0
        assert data['ready'] is False


# ── read-only, and the panel ────────────────────────────────────────
def test_it_writes_nothing():
    """Safe to run against production at any time."""
    from app.models.audit import AuditEvent

    with flask_app.app_context():
        _chain_on(PILOT)
        before = AuditEvent.query.count()
        pr.report()
        pr.report()
        assert AuditEvent.query.count() == before


def test_the_admin_panel_renders_for_an_admin():
    with flask_app.app_context():
        _chain_on(PILOT)
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s['emp_code'] = ADMIN
        s['role'] = 'admin'
    r = client.get('/admin/crm-rules')
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    html = r.get_data(as_text=True)
    assert 'CRM rules' in html
    assert 'rfq_coordinator' in html
    assert VERTICAL in html


def test_the_panel_is_closed_to_everybody_else():
    with flask_app.app_context():
        _chain_on(PILOT)
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s['emp_code'] = PILOT
        s['role'] = 'user'
    assert client.get('/admin/crm-rules').status_code == 403


def test_the_panel_and_the_script_read_the_same_thing():
    """A readiness check that disagrees with itself is worse than
    none."""
    with flask_app.app_context():
        _chain_on(PILOT)
        direct = pr.report()
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s['emp_code'] = ADMIN
        s['role'] = 'admin'
    body = client.get('/api/admin/crm-rules').get_json()
    assert body['missing'] == direct['missing']
    assert list(body['verticals']) == list(direct['verticals'])
