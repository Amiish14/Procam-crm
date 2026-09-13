"""
Routes that used to ask "is the role called admin?" now ask the Access
Matrix. An administrator the matrix has narrowed loses what was taken
away; a person the matrix has granted gains it.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'MatrixLegacyTestOnly12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'matrixlegacy.db'))

from app import app as flask_app, db, Employee, Opportunity       # noqa: E402
from app.access.service import set_profile, ALL_PERMS              # noqa: E402
from app.models.access import DataScope                            # noqa: E402

flask_app.config['WTF_CSRF_ENABLED'] = False


@pytest.fixture()
def people():
    with flask_app.app_context():
        db.create_all()
        for code, role, vert in (('MLNARROW', 'admin', 'All'),
                                 ('MLGRANT', 'user', 'All'),
                                 ('MLHEAD', 'user', 'Warehousing'),
                                 ('MLREP', 'user', 'Warehousing'),
                                 ('MLOTHER', 'user', 'Installation')):
            e = Employee.query.filter_by(emp_code=code).first()
            if e is None:
                e = Employee(emp_code=code, name=code)
                db.session.add(e)
            e.role, e.vertical, e.is_active = role, vert, True
            e.must_change_pw, e.is_super_admin, e.session_version = \
                False, False, 0
        db.session.commit()
        # an admin by role, narrowed by the matrix to no admin rights
        set_profile('MLNARROW', DataScope.OWN, [], actor='SYSTEM')
        # a user by role, granted employee administration and triage
        set_profile('MLGRANT', DataScope.ALL,
                    ['admin.employees', 'admin.triage'], actor='SYSTEM')
        set_profile('MLHEAD', DataScope.VERTICAL, [], actor='SYSTEM')
        set_profile('MLREP', DataScope.OWN, [], actor='SYSTEM')
        set_profile('MLOTHER', DataScope.OWN, [], actor='SYSTEM')
        opp = Opportunity(opp_number='ML-1', owner_emp_code='MLREP',
                          stage='Proposal')
        db.session.add(opp)
        db.session.commit()
        oid = opp.id
    yield {'opp': oid}
    with flask_app.app_context():
        Opportunity.query.filter_by(id=oid).delete()
        Employee.query.filter_by(emp_code='MLNEW01').delete()
        db.session.commit()


def _c(code, role):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role=role, vertical='All', sv=0)
    return c


def test_a_narrowed_admin_loses_employee_administration(people):
    r = _c('MLNARROW', 'admin').post('/api/employees', json={
        'emp_code': 'MLNEW01', 'name': 'New'})
    assert r.status_code == 403 and r.get_json()['need'] == 'admin.employees'
    assert _c('MLNARROW', 'admin').get('/api/news').status_code == 403


def test_a_granted_user_gains_it(people):
    r = _c('MLGRANT', 'user').post('/api/employees', json={
        'emp_code': 'MLNEW01', 'name': 'New'})
    assert r.status_code == 200
    full = _c('MLGRANT', 'user').get('/api/employees').get_json()
    assert 'must_change_pw' in full[0]         # the full records, not names


def test_opportunity_edits_follow_data_scope(people):
    oid = people['opp']
    assert _c('MLHEAD', 'user').put(f'/api/opportunities/{oid}',
                                    json={'probability': 40}).status_code == 200
    assert _c('MLOTHER', 'user').put(f'/api/opportunities/{oid}',
                                     json={'probability': 90}).status_code == 403


def test_ownership_transfer_needs_triage_rights(people):
    oid = people['opp']
    r = _c('MLHEAD', 'user').put(f'/api/opportunities/{oid}',
                                 json={'owner_emp_code': 'MLHEAD'})
    assert r.status_code == 403
    r = _c('MLGRANT', 'user').put(f'/api/opportunities/{oid}',
                                  json={'owner_emp_code': 'MLHEAD'})
    assert r.status_code == 200


def test_email_administration_needs_the_email_permission(people):
    r = _c('MLNARROW', 'admin').get('/api/email/inbox')
    assert r.status_code == 403


def test_without_the_permission_only_names_are_listed(people):
    rows = _c('MLREP', 'user').get('/api/employees').get_json()
    assert rows and 'must_change_pw' not in rows[0] and 'email' not in rows[0]


def test_subscription_management_needs_the_email_permission(people):
    assert _c('MLNARROW', 'admin').post(
        '/api/email/subscribe').status_code == 403


def test_two_employees_without_email_can_be_created(people):
    c = _c('MLGRANT', 'user')
    with flask_app.app_context():
        Employee.query.filter(Employee.emp_code.in_(['MLNOMAIL1',
                                                     'MLNOMAIL2'])).delete()
        db.session.commit()
    for code in ('MLNOMAIL1', 'MLNOMAIL2'):
        r = c.post('/api/employees', json={'emp_code': code, 'name': code})
        assert r.status_code == 200, r.get_data(as_text=True)[:200]
    with flask_app.app_context():
        Employee.query.filter(Employee.emp_code.in_(['MLNOMAIL1',
                                                     'MLNOMAIL2'])).delete()
        db.session.commit()
