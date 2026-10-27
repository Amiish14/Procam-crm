"""
Every new page, rendered with its flag ON.

Two pages reached five pilot users as a 500 because the routes
were written, the flags were switched on, and the templates did
not exist. Every test passed: they all asserted the 404 a
flag-off state gives, which is the one path that never touches a
template.

So each page here is fetched the way a pilot user fetches it —
flag on, signed in, real data underneath — and the body is
checked for something only a working render produces. A 200 with
an empty body would pass a status check and fail a person.
"""
import importlib
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'pilotpage-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'PilotPage-12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'pilotpage.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
importlib.import_module('app.models.rfq')
importlib.import_module('app.models.rfq_target')
flask_app = _main.app
flask_app.config['WTF_CSRF_ENABLED'] = False
db = _main.db
Company, Employee, Lead = _main.Company, _main.Employee, _main.Lead

from app.models.rfq import RFQ                            # noqa: E402
from app.services import rfq_chain as rc                  # noqa: E402

PILOT = 'PP_PILOT'
FLAGS = ('FEATURE_ACCOUNT_FIRST', 'FEATURE_RFQ_CHAIN',
         'FEATURE_QUOTE_REV_V2', 'FEATURE_PRE_RFQ_FUNNEL',
         'FEATURE_LEAD_JOURNEY', 'FEATURE_RFQ_REPORTS')


def _purge():
    from app.models.task_engine import TaskInstance
    TaskInstance.query.filter(
        TaskInstance.entity_type == 'RFQ').delete(
            synchronize_session=False)
    RFQ.query.filter(RFQ.rfq_number.like('PP-%')).delete(
        synchronize_session=False)
    Lead.query.filter(Lead.company.like('PP %')).delete(
        synchronize_session=False)
    Company.query.filter(Company.name.like('PP %')).delete(
        synchronize_session=False)
    Employee.query.filter(Employee.emp_code == PILOT).delete(
        synchronize_session=False)
    _flags(False)
    db.session.commit()


def _flags(state):
    import json
    from app.models.integration import AppSetting
    from app.services import flags
    for name in FLAGS:
        key = 'flag.' + name
        row = AppSetting.query.filter_by(key=key).first()
        if row is None:
            row = AppSetting(key=key)
            db.session.add(row)
        row.value_json = json.dumps(bool(state))
    db.session.commit()
    flags.cache_clear()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        db.session.add(Employee(
            emp_code=PILOT, name='Pilot Person', password_hash='x',
            role='admin', vertical='Warehousing',
            email='pp@procamlogistics.com'))
        db.session.commit()
        _flags(True)
        yield
        _purge()


@pytest.fixture
def world():
    """A lead with an RFQ and one open step — enough to render."""
    with flask_app.app_context():
        account = Company(name='PP Steel', is_active=True)
        db.session.add(account)
        db.session.flush()
        lead = Lead(company='PP Steel', stage='RFQ Generated',
                    source='manual', procam_vertical='Warehousing',
                    assigned_to=PILOT, company_id=account.id,
                    created_at=datetime.utcnow())
        db.session.add(lead)
        db.session.flush()
        rfq = RFQ(rfq_number='PP-RFQ-1', subject='PP job',
                  lead_id=lead.id, account_id=account.id,
                  priority='Medium', lead_driver=PILOT,
                  created_by_id=PILOT, needs_technical=True,
                  needs_survey=True)
        db.session.add(rfq)
        db.session.flush()
        task = rc.open_step(rfq, 6, owner=PILOT, actor='test')
        task.started_at = datetime.utcnow() - timedelta(hours=20)
        db.session.commit()
        return {'lead_id': lead.id, 'rfq_id': rfq.id}


def _client():
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['emp_code'] = PILOT
        s['role'] = 'admin'
    return c


def _body(path):
    r = _client().get(path)
    assert r.status_code == 200, (
        f'{path} -> {r.status_code}: '
        f'{r.get_data(as_text=True)[:200]}')
    return r.get_data(as_text=True)


# ── each page renders, with something only a real render makes ──────
def test_the_chain_board_renders():
    html = _body('/rfqs/chain')
    assert '<title>RFQ Chain' in html
    assert 'api/chain/board' in html


def test_the_reports_page_renders():
    html = _body('/rfqs/reports')
    assert '<title>RFQ Reports' in html
    assert 'api/reports/tiles' in html
    assert 'Workload and completion' in html


def test_the_lead_record_renders(world):
    html = _body(f'/leads/{world["lead_id"]}/journey')
    assert '<title>Lead journey' in html
    assert 'Before the RFQ' in html and 'RFQ to order' in html
    # §6.4's pieces, not just the shell
    for piece in ('nextAction', 'showStep', 'Conditions', 'Quotes',
                  'Audit', 'Open TMS IOU form'):
        assert piece in html, piece


def test_the_account_check_screen_renders():
    html = _body('/accounts/check')
    assert 'Account check' in html


def test_my_work_renders_with_the_chain_bucket(world):
    html = _body('/my-work/tasks')
    assert 'Waiting on me — RFQ steps' in html
    assert 'PP-RFQ-1' in html
    assert '>Act<' in html


def test_every_pilot_page_includes_the_csrf_helper():
    """A page that writes without a token does nothing, silently."""
    for path in ('/rfqs/chain', '/rfqs/reports', '/accounts/check'):
        assert '__crmCsrfFetch' in _body(path), path


# ── the data behind them ─────────────────────────────────────────────
def test_the_json_endpoints_answer_for_a_pilot_user(world):
    client = _client()
    for path in ('/api/chain/board', '/api/reports/tiles',
                 '/api/reports/workload', '/api/reports/cockpit',
                 f'/api/journey/{world["lead_id"]}',
                 f'/api/journey/{world["lead_id"]}/step/6',
                 f'/api/journey/{world["lead_id"]}/tab/conditions',
                 f'/api/journey/{world["lead_id"]}/tab/quotes',
                 f'/api/journey/{world["lead_id"]}/tab/audit'):
        r = client.get(path)
        assert r.status_code == 200, f'{path} -> {r.status_code}'
        assert r.get_json().get('ok') is True, path


def test_my_work_json_carries_the_chain_steps(world):
    body = _client().get('/my-work.json').get_json()
    assert body['ok'] is True
    steps = body['chain_steps']
    assert steps and steps[0]['step_no'] == 6
    assert steps[0]['rfq_number'] == 'PP-RFQ-1'
    # The Act button must go somewhere real.
    assert steps[0]['act'] == f'/leads/{world["lead_id"]}/journey?step=6'


def test_the_next_action_names_the_open_step(world):
    body = _client().get(
        f'/api/journey/{world["lead_id"]}').get_json()
    nxt = body['next_action']
    assert nxt['step_no'] == 6
    assert nxt['can_act'] is True
    assert nxt['words']


def test_step_detail_says_who_and_how_long(world):
    body = _client().get(
        f'/api/journey/{world["lead_id"]}/step/6').get_json()
    step = body['step']
    assert step['state'] == 'current'
    assert step['owner'] == PILOT
    assert step['hours'] >= 19
    assert step['standard']


# ── and with the flags off, nothing is there ─────────────────────────
def test_with_the_flags_off_the_new_pages_are_gone(world):
    with flask_app.app_context():
        _flags(False)
    client = _client()
    for path in ('/rfqs/chain', '/rfqs/reports', '/accounts/check',
                 f'/leads/{world["lead_id"]}/journey'):
        assert client.get(path).status_code == 404, path
    # My Work still works; it simply has no chain bucket.
    html = client.get('/my-work/tasks').get_data(as_text=True)
    assert 'Waiting on me — RFQ steps' not in html
