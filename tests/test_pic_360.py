"""PIC 360 — §22.

"A manager clicking any Procam PIC should see the person's AUTHORISED
commercial workload." Authorised is the part that needs testing: a vertical
head must not gain visibility here that they would be refused in reports.
"""
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'PicTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'pic.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Lead, Opportunity, Employee = (_main.Company, _main.Lead,
                                        _main.Opportunity, _main.Employee)

from app.pic360 import service as p360                      # noqa: E402
from app.models.task_engine import TaskInstance             # noqa: E402


@pytest.fixture(scope='module')
def team():
    with flask_app.app_context():
        db.create_all()
        spec = (
            ('PADMIN', 'Pic Admin',  'admin', 'All',             True,  False),
            ('PHEAD',  'Heavy Head', 'user',  'Heavy Transport',  False, True),
            ('PREP',   'Heavy Rep',  'user',  'Heavy Transport',  False, False),
            ('POTHER', 'Other Rep',  'user',  'Warehousing',      False, False),
        )
        for code, name, role, vert, sup, head in spec:
            e = Employee.query.filter_by(emp_code=code).first()
            if not e:
                e = Employee(emp_code=code, name=name)
                db.session.add(e)
            e.name, e.role, e.vertical = name, role, vert
            e.is_active, e.must_change_pw = True, False
            e.is_super_admin, e.is_vertical_head = sup, head
        db.session.commit()

        acme = Company(name='Acme', is_active=True, pic_emp_code='PREP')
        db.session.add(acme)
        db.session.flush()

        for n in range(3):
            db.session.add(Lead(company='Acme', company_id=acme.id,
                                assigned_to='PREP', stage='New'))
        db.session.add(Opportunity(opp_number='P-1', company_id=acme.id,
                                   stage='Won', value_inr=200000,
                                   owner_emp_code='PREP'))
        db.session.add(Opportunity(opp_number='P-2', company_id=acme.id,
                                   stage='Lost', owner_emp_code='PREP'))
        db.session.add(TaskInstance(task_key='lead.qualify',
                                    entity_type='Lead', entity_id=1,
                                    owner_user_id='PREP', status='Pending',
                                    priority=3))
        db.session.commit()
        return True


def _c(code):
    c = flask_app.test_client()
    with flask_app.app_context():
        e = Employee.query.filter_by(emp_code=code).first()
        role, vert = e.role, e.vertical or ''
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role=role, vertical=vert)
    return c


def test_workload_is_computed_from_real_records(team):
    with flask_app.app_context():
        w = p360.workload('PREP')
    assert w['leads'] == 3
    assert w['accounts'] == 1
    assert w['opportunities'] == 2
    assert w['won'] == 1 and w['lost'] == 1
    assert w['won_value'] == 200000
    assert w['win_rate'] == 50.0
    assert w['open_tasks'] == 1


def test_admin_sees_anyone(team):
    assert _c('PADMIN').get('/people/PREP').status_code == 200
    assert _c('PADMIN').get('/people/POTHER').status_code == 200


def test_a_head_sees_their_own_vertical(team):
    assert _c('PHEAD').get('/people/PREP').status_code == 200


def test_a_head_cannot_see_another_vertical(team):
    """The point of "authorised" — this must match the reports' scope."""
    r = _c('PHEAD').get('/people/POTHER')
    assert r.status_code == 403, \
        'a vertical head reached someone outside their vertical'


def test_the_people_list_is_scoped(team):
    body = _c('PHEAD').get('/people').get_data(as_text=True)
    assert 'Heavy Rep' in body
    assert 'Other Rep' not in body, 'the list leaked another vertical'


def test_admin_list_shows_everyone(team):
    body = _c('PADMIN').get('/people').get_data(as_text=True)
    assert 'Heavy Rep' in body and 'Other Rep' in body


def test_api_enforces_the_same_rule(team):
    """A UI check that the API does not share is not a check at all."""
    assert _c('PHEAD').get('/api/people/POTHER/360').status_code == 403
    assert _c('PHEAD').get('/api/people/PREP/360').status_code == 200


def test_unknown_person_is_a_clean_404(team):
    assert _c('PADMIN').get('/people/NOBODY').status_code == 404


def test_anonymous_is_redirected(team):
    assert flask_app.test_client().get('/people/PREP').status_code == 302
