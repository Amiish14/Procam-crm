"""Numbers open the records behind them, the same way everywhere.

A count with no route to what it counts is a complaint, not a tool — and a
count that IS clickable but does not look it may as well not be. These
cover both: the routes exist, and the affordance is consistent.
"""
import os
import re
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_PREFIX = '/CRM'
os.environ['URL_PREFIX'] = _PREFIX
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'DrillUniTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'drilluni.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Lead, Opportunity, Employee = (_main.Company, _main.Lead,
                                        _main.Opportunity, _main.Employee)

_TEMPLATES = os.path.join(_ROOT, 'templates')
_CSS = os.path.join(_ROOT, 'static', 'css', 'crm.css')


@pytest.fixture(autouse=True)
def _prefix():
    previous = os.environ.get('URL_PREFIX')
    os.environ['URL_PREFIX'] = _PREFIX
    yield
    if previous is None:
        os.environ.pop('URL_PREFIX', None)
    else:
        os.environ['URL_PREFIX'] = previous


@pytest.fixture(scope='module')
def world():
    with flask_app.app_context():
        db.create_all()
        e = Employee.query.filter_by(emp_code='DUADM').first()
        if not e:
            e = Employee(emp_code='DUADM', name='Drill Uniform')
            db.session.add(e)
        e.role, e.is_active, e.must_change_pw = 'admin', True, False
        e.is_super_admin, e.vertical = True, 'All'
        c = Company.query.filter_by(name='Drill Uniform Co').first()
        if not c:
            c = Company(name='Drill Uniform Co', is_active=True)
            db.session.add(c)
            db.session.flush()
            db.session.add(Lead(company='Drill Uniform Co', company_id=c.id,
                                stage='New'))
            db.session.add(Opportunity(opp_number='DU-1', company_id=c.id,
                                       stage='Won', value_inr=1000,
                                       owner_emp_code='DUADM'))
        # Tasks-by-User has nothing to link without a task.
        from app.models.task_engine import TaskInstance
        if not TaskInstance.query.filter_by(owner_user_id='DUADM').first():
            db.session.add(TaskInstance(
                task_key='lead.qualify', entity_type='Lead', entity_id=1,
                entity_display='Drill Uniform Co', owner_user_id='DUADM',
                status='Pending', priority=3))
        db.session.commit()
        return c.id


def _c():
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='DUADM', name='Drill Uniform', role='admin',
                 vertical='All')
    return c


def _get(path):
    return _c().get(path, environ_overrides={'SCRIPT_NAME': _PREFIX}
                    ).get_data(as_text=True)


# ── every Data Quality number opens its records ──────────────────────
def test_every_data_quality_check_has_a_records_view(world):
    from app.data_quality import service as dq
    client = _c()
    with flask_app.app_context():
        keys = [c[0] for c in dq.CHECKS]
    for key in keys:
        r = client.get(f'/admin/data-quality/{key}',
                       environ_overrides={'SCRIPT_NAME': _PREFIX})
        assert r.status_code == 200, f'{key} → {r.status_code}'


def test_data_quality_counts_link_to_their_records(world):
    html = _get('/admin/data-quality')
    assert '/admin/data-quality/unowned_leads' in html, \
        'the number must be a route to the records'


def test_an_unknown_check_is_404(world):
    r = _c().get('/admin/data-quality/not_a_check',
                 environ_overrides={'SCRIPT_NAME': _PREFIX})
    assert r.status_code == 404


# ── the 360 tiles reach their detail ─────────────────────────────────
def test_company_360_tiles_open_their_sections(world):
    html = _get(f'/companies/{world}')
    for anchor in ('#sec-opps', '#sec-people', '#sec-leads', '#sec-timeline'):
        assert f'href="{anchor}"' in html, f'no tile opens {anchor}'
        assert f'id="{anchor[1:]}"' in html, f'{anchor} has no target'


def test_pic_360_tiles_open_their_sections(world):
    html = _get('/people/DUADM')
    for anchor in ('#sec-tasks', '#sec-accounts'):
        assert f'href="{anchor}"' in html
        assert f'id="{anchor[1:]}"' in html


# ── reports drill through, via the one shared renderer ───────────────
def test_report_owner_columns_link_to_that_person(world):
    html = _get('/reports/tasks-by-user')
    assert f'{_PREFIX}/people/' in html, \
        'an employee code in a report should open their PIC 360'


def test_report_account_columns_link_to_company_master(world):
    html = _get('/reports/won-value-by-account')
    assert f'{_PREFIX}/companies?q=' in html


def test_the_unattributed_row_is_not_linked(world):
    """"(not linked to an account — N Won deals)" is not a company."""
    src = open(os.path.join(_TEMPLATES, 'reports_v2',
                            '_generic.html')).read()
    assert "not v.startswith('(')" in src


def test_training_rows_open_the_person(world):
    html = _get('/admin/training')
    assert f'{_PREFIX}/people/' in html


# ── the affordance is the same everywhere ────────────────────────────
def test_a_drillable_tile_looks_drillable():
    css = open(_CSS).read()
    assert 'a.kpi::after' in css, 'no visual cue that a tile opens'
    assert 'a.kpi:hover' in css
    assert 'tbody tr[onclick]{ cursor:pointer; }' in css, \
        'a clickable row must show it is clickable'


def test_tiles_do_not_look_like_hyperlinks():
    """They should read as tiles that open, not as links."""
    css = open(_CSS).read()
    block = css[css.index('a.kpi, a.tile{'):]
    assert 'text-decoration:none' in block[:200]
    assert 'color:inherit' in block[:200]
