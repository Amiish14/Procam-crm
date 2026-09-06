"""The Access Control screen must be able to grant and revoke report access,
and only an admin may do it.
"""
import os
import sys
import json
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'AccessTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'access.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db, Employee = _main.app, _main.db, _main.Employee

# CSRF stays on in production; the test client has no token, and what is
# under test here is the admin check, not the CSRF check.
flask_app.config['WTF_CSRF_ENABLED'] = False


@pytest.fixture(scope='module')
def people():
    with flask_app.app_context():
        db.create_all()
        made = {}
        for code, name, role, vert in (
                ('AADMIN', 'Access Admin', 'admin', 'All'),
                ('AUSER',  'Access User',  'user',  'Warehousing'),
                ('ANOVERT', 'No Vertical', 'user', None)):
            e = Employee.query.filter_by(emp_code=code).first()
            if not e:
                e = Employee(emp_code=code, name=name)
                db.session.add(e)
            e.name, e.role, e.vertical = name, role, vert
            e.is_active, e.is_vertical_head = True, False
            # /app redirects to a forced password change otherwise
            e.must_change_pw = False
            db.session.commit()
            made[code] = e.id
        return made


def _client(emp_code):
    c = flask_app.test_client()
    with flask_app.app_context():
        e = Employee.query.filter_by(emp_code=emp_code).first()
        role, vert = e.role, e.vertical or ''
    with c.session_transaction() as sess:
        sess.update(emp_code=emp_code, name=emp_code, role=role, vertical=vert)
    return c


def _head(code):
    with flask_app.app_context():
        return Employee.query.filter_by(emp_code=code).first().is_vertical_head


def test_admin_can_grant_and_revoke(people):
    c, eid = _client('AADMIN'), people['AUSER']
    assert _head('AUSER') is False

    r = c.put(f'/api/employees/{eid}',
              data=json.dumps({'is_vertical_head': True}),
              content_type='application/json')
    assert r.status_code == 200, r.get_data(as_text=True)
    assert _head('AUSER') is True, 'grant did not persist'

    r = c.put(f'/api/employees/{eid}',
              data=json.dumps({'is_vertical_head': False}),
              content_type='application/json')
    assert r.status_code == 200
    assert _head('AUSER') is False, 'revoke did not persist'


def test_non_admin_cannot_grant_itself_access(people):
    """The whole gate is worthless if a user can flag themselves."""
    c, eid = _client('AUSER'), people['AUSER']
    r = c.put(f'/api/employees/{eid}',
              data=json.dumps({'is_vertical_head': True}),
              content_type='application/json')
    assert r.status_code in (401, 403), \
        f'non-admin escalated to vertical head ({r.status_code})'
    assert _head('AUSER') is False


def test_api_me_exposes_the_flag(people):
    """The nav gate reads this field; without it the Reports entry never shows."""
    body = _client('AADMIN').get('/api/me').get_json()
    assert 'is_vertical_head' in body


def test_access_page_is_present_for_admin(people):
    html = _client('AADMIN').get('/app').get_data(as_text=True)
    assert 'pg-access' in html
    assert 'Access Control' in html
