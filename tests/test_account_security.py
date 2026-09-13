"""
Account hardening: the super admin cannot be taken over, passwords are not
guessable, and a session obeys the account behind it.

Each test states the attack it closes. Before this module:
  * any admin could reset the super admin's password to its employee code
  * every new or reset password WAS the employee code
  * a deactivated employee's cookie kept working for eight hours
  * must_change_pw was enforced by one HTML page and no API route
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'AcctSecTestOnly12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'acctsec.db'))

from app import app as flask_app, db, Employee                # noqa: E402

flask_app.config['WTF_CSRF_ENABLED'] = False


def _emp(code, *, role='user', super_=False, active=True, pw=None):
    e = Employee.query.filter_by(emp_code=code).first()
    if e is None:
        e = Employee(emp_code=code, name=code)
        db.session.add(e)
    e.role, e.is_active, e.must_change_pw = role, active, False
    e.vertical, e.is_super_admin = 'All', super_
    e.set_password(pw or 'SomethingLong123')
    db.session.commit()
    return e


@pytest.fixture()
def people():
    with flask_app.app_context():
        db.create_all()
        return {c: _emp(c, **kw).id for c, kw in (
            ('ASSUPER', {'role': 'admin', 'super_': True}),
            ('ASADMIN', {'role': 'admin'}),
            ('ASREP', {}),
        )}


def _client(code, role='user', **extra):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role=role, vertical='All', **extra)
    return c


# ── the super admin ──────────────────────────────────────────────────
def test_an_admin_cannot_reset_the_super_admins_password(people):
    r = _client('ASADMIN', 'admin').put(
        f'/api/employees/{people["ASSUPER"]}', json={'reset_password': True})
    assert r.status_code == 403
    with flask_app.app_context():
        e = db.session.get(Employee, people['ASSUPER'])
        assert e.check_password('SomethingLong123')


def test_an_admin_cannot_demote_or_deactivate_the_super_admin(people):
    c = _client('ASADMIN', 'admin')
    assert c.put(f'/api/employees/{people["ASSUPER"]}',
                 json={'role': 'user'}).status_code == 403
    assert c.delete(f'/api/employees/{people["ASSUPER"]}').status_code == 403
    with flask_app.app_context():
        e = db.session.get(Employee, people['ASSUPER'])
        assert e.role == 'admin' and e.is_active


def test_the_super_admin_can_still_manage_its_own_account(people):
    r = _client('ASSUPER', 'admin').put(
        f'/api/employees/{people["ASSUPER"]}', json={'mobile': '999'})
    assert r.status_code == 200


def test_an_admin_can_still_manage_ordinary_accounts(people):
    r = _client('ASADMIN', 'admin').put(
        f'/api/employees/{people["ASREP"]}', json={'designation': 'Lead'})
    assert r.status_code == 200


# ── passwords ────────────────────────────────────────────────────────
def test_a_reset_password_is_random_not_the_employee_code(people):
    r = _client('ASADMIN', 'admin').put(
        f'/api/employees/{people["ASREP"]}', json={'reset_password': True})
    temp = r.get_json()['temp_password']
    assert len(temp) >= 12 and temp.lower() != 'asrep'
    with flask_app.app_context():
        e = db.session.get(Employee, people['ASREP'])
        assert not e.check_password('asrep')
        assert e.check_password(temp)
        assert e.must_change_pw is True


def test_two_resets_do_not_give_the_same_password(people):
    c = _client('ASADMIN', 'admin')
    a = c.put(f'/api/employees/{people["ASREP"]}',
              json={'reset_password': True}).get_json()['temp_password']
    b = c.put(f'/api/employees/{people["ASREP"]}',
              json={'reset_password': True}).get_json()['temp_password']
    assert a != b


def test_a_new_employee_does_not_get_the_code_as_password(people):
    with flask_app.app_context():
        Employee.query.filter_by(emp_code='ASNEW01').delete()
        db.session.commit()
    r = _client('ASADMIN', 'admin').post(
        '/api/employees', json={'emp_code': 'ASNEW01', 'name': 'New Person'})
    body = r.get_json()
    assert 'asnew01' not in body['message']
    with flask_app.app_context():
        e = Employee.query.filter_by(emp_code='ASNEW01').first()
        assert not e.check_password('asnew01')
        assert e.check_password(body['temp_password'])


def test_a_short_password_or_the_employee_code_is_refused(people):
    c = _client('ASREP')
    for bad in ('short1', 'asrep', 'ASREP'):
        r = c.post('/change-password', json={'current': 'SomethingLong123',
                                             'new_password': bad})
        assert r.status_code == 400, bad


# ── the session obeys the account ────────────────────────────────────
def test_a_deactivated_employee_is_logged_out_on_the_next_request(people):
    c = _client('ASREP')
    assert c.get('/api/leads').status_code == 200
    with flask_app.app_context():
        db.session.get(Employee, people['ASREP']).is_active = False
        db.session.commit()
    r = c.get('/api/leads')
    assert r.status_code == 401 and r.get_json()['code'] == 'inactive'
    # The session the server sends back no longer names the employee.
    # Decoded from the response rather than read from the test client's
    # jar: the cookie is Secure, and the client on plain http ignores
    # it, which a browser on https would not.
    signer = flask_app.session_interface.get_signing_serializer(flask_app)
    sent = [h.split(';', 1)[0].split('=', 1)[1]
            for h in r.headers.getlist('Set-Cookie')
            if h.startswith(flask_app.config['SESSION_COOKIE_NAME'] + '=')]
    assert sent, 'the session cookie was not rewritten'
    assert 'emp_code' not in (signer.loads(sent[0]) if sent[0] else {})


def test_a_default_password_session_cannot_use_the_api(people):
    c = _client('ASREP', must_change_pw=True)
    r = c.get('/api/leads')
    assert r.status_code == 403
    assert r.get_json()['code'] == 'password_change_required'
    # and changing it is exactly what unlocks it
    r = c.post('/change-password', json={'current': 'SomethingLong123',
                                         'new_password': 'ANewLongPassword9'})
    assert r.status_code == 200
    assert c.get('/api/leads').status_code == 200


def test_login_carries_must_change_into_the_session(people):
    with flask_app.app_context():
        e = db.session.get(Employee, people['ASREP'])
        e.must_change_pw = True
        db.session.commit()
    c = flask_app.test_client()
    r = c.post('/login', json={'emp_code': 'ASREP',
                               'password': 'SomethingLong123'})
    assert r.get_json()['must_change'] is True
    assert c.get('/api/leads').status_code == 403


def test_a_long_employee_code_is_still_refused_as_a_password(people):
    """Length alone would let a code of ten or more characters through."""
    with flask_app.app_context():
        _emp('ASLONGCODE01')
    r = _client('ASLONGCODE01').post(
        '/change-password', json={'current': 'SomethingLong123',
                                  'new_password': 'aslongcode01'})
    assert r.status_code == 400
    assert 'employee code' in r.get_json()['error']
