"""
Sign-in and session hardening.

  * repeated failures lock the account, counted in the database so the
    limit holds across workers
  * an administrator's temporary password expires
  * common passwords and passwords containing the employee code are refused
  * a password reset, deactivation or "sign out everywhere" ends every
    other session
  * a role change takes effect on the next request, not the next sign-in
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'SigninTestOnly12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'signin.db'))

import app as app_module                                       # noqa: E402
from app import app as flask_app, db, Employee                 # noqa: E402

flask_app.config['WTF_CSRF_ENABLED'] = False
PW = 'Harbour-Crane-4471'


@pytest.fixture(autouse=True)
def no_ip_limit():
    """The per-IP limiter would stop these tests before the per-account
    lock they exist to test."""
    before = app_module.limiter.enabled
    app_module.limiter.enabled = False
    yield
    app_module.limiter.enabled = before


def _emp(code, role='user'):
    with flask_app.app_context():
        db.create_all()
        e = Employee.query.filter_by(emp_code=code).first()
        if e is None:
            e = Employee(emp_code=code, name=code)
            db.session.add(e)
        e.role, e.is_active, e.must_change_pw = role, True, False
        e.vertical, e.is_super_admin = 'All', role == 'admin'
        e.session_version, e.failed_logins, e.locked_until = 0, 0, None
        e.temp_password_expires_at = None
        e.set_password(PW)
        db.session.commit()
        return e.id


def _client():
    """Requests arrive over https, as in production, so the Secure session
    cookie a response sets is kept by the test client."""
    c = flask_app.test_client()
    c.environ_base['wsgi.url_scheme'] = 'https'
    return c


def _login(code, pw=PW):
    c = _client()
    r = c.post('/login', json={'emp_code': code, 'password': pw})
    return c, r


def _as(code, role):
    c = _client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role=role, vertical='All', sv=0)
    return c


# ── lockout ──────────────────────────────────────────────────────────
def test_eight_failures_lock_the_account():
    _emp('SGLOCK')
    for _ in range(app_module.LOGIN_LOCK_AFTER):
        _c, r = _login('SGLOCK', 'wrong-password-x')
        assert r.status_code in (401, 429)
    _c, r = _login('SGLOCK')                      # the right password
    assert r.status_code == 429
    assert r.get_json()['code'] == 'locked'


def test_the_lock_expires():
    eid = _emp('SGEXP')
    with flask_app.app_context():
        e = db.session.get(Employee, eid)
        e.locked_until = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
    _c, r = _login('SGEXP')
    assert r.status_code == 200


def test_a_success_clears_the_failure_count():
    eid = _emp('SGRESET')
    for _ in range(3):
        _login('SGRESET', 'wrong-password-x')
    _login('SGRESET')
    with flask_app.app_context():
        assert db.session.get(Employee, eid).failed_logins == 0


def test_an_unknown_code_does_not_lock_anyone():
    _c, r = _login('NOSUCHCODE', 'anything-at-all')
    assert r.status_code == 401


# ── temporary passwords ──────────────────────────────────────────────
def test_an_expired_temporary_password_is_refused():
    _emp('SGADM', 'admin')
    eid = _emp('SGTEMP')
    r = _as('SGADM', 'admin').put(f'/api/employees/{eid}',
                                  json={'reset_password': True})
    temp = r.get_json()['temp_password']
    _c, ok = _login('SGTEMP', temp)
    assert ok.status_code == 200
    with flask_app.app_context():
        e = db.session.get(Employee, eid)
        assert e.temp_password_expires_at > datetime.utcnow()
        e.temp_password_expires_at = datetime.utcnow() - timedelta(minutes=1)
        db.session.commit()
    _c, r = _login('SGTEMP', temp)
    assert r.status_code == 401 and r.get_json()['code'] == 'temp_expired'


# ── password rules ───────────────────────────────────────────────────
@pytest.mark.parametrize('bad', ['Password2026!', 'procam@12345', 'Welcome@123',
                                 'xSGRULESx-2026', 'aaaaaaaaaaaa'])
def test_weak_passwords_are_refused(bad):
    _emp('SGRULES')
    c, _ = _login('SGRULES')
    r = c.post('/change-password', json={'current': PW, 'new_password': bad})
    assert r.status_code == 400, bad


def test_a_good_password_is_accepted_and_the_same_one_is_not():
    _emp('SGGOOD')
    c, _ = _login('SGGOOD')
    assert c.post('/change-password', json={
        'current': PW, 'new_password': PW}).status_code == 400
    assert c.post('/change-password', json={
        'current': PW, 'new_password': 'Tidal-Barge-9031'}).status_code == 200


# ── session revocation ───────────────────────────────────────────────
def test_an_admin_reset_ends_the_holders_other_sessions():
    _emp('SGADM', 'admin')
    eid = _emp('SGREV')
    holder, _ = _login('SGREV')
    assert holder.get('/api/leads').status_code == 200
    _as('SGADM', 'admin').put(f'/api/employees/{eid}',
                              json={'reset_password': True})
    r = holder.get('/api/leads')
    assert r.status_code == 401 and r.get_json()['code'] == 'session_revoked'


def test_changing_your_password_keeps_this_session_and_ends_the_others():
    _emp('SGSELF')
    here, _ = _login('SGSELF')
    there, _ = _login('SGSELF')
    assert here.post('/change-password', json={
        'current': PW, 'new_password': 'Quay-Lashing-5520'}).status_code == 200
    assert here.get('/api/leads').status_code == 200
    assert there.get('/api/leads').status_code == 401


def test_sign_out_everywhere():
    _emp('SGALL')
    here, _ = _login('SGALL')
    there, _ = _login('SGALL')
    assert here.post('/api/account/sessions/revoke').status_code == 200
    assert here.get('/api/leads').status_code == 200
    assert there.get('/api/leads').status_code == 401


def test_a_session_from_before_versioning_still_works():
    """Sessions issued before this release carry no version. They must not
    all be signed out by the deploy."""
    _emp('SGOLD')
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='SGOLD', name='SGOLD', role='user', vertical='All')
    assert c.get('/api/leads').status_code == 200


# ── live role ────────────────────────────────────────────────────────
def test_a_demotion_takes_effect_on_the_next_request():
    eid = _emp('SGDEMOTE', 'admin')
    c, _ = _login('SGDEMOTE')
    assert c.get('/api/news').status_code == 200          # admin-only
    with flask_app.app_context():
        e = db.session.get(Employee, eid)
        e.role, e.is_super_admin = 'user', False
        db.session.commit()
    assert c.get('/api/news').status_code == 403


def test_the_session_cookie_always_has_a_real_path():
    """URL_PREFIX set but empty produced "Path=" on the session cookie."""
    c = _client()
    r = c.post('/login', json={'emp_code': _code_for_path_test(),
                               'password': PW})
    cookies = [h for h in r.headers.getlist('Set-Cookie')
               if h.startswith(flask_app.config['SESSION_COOKIE_NAME'] + '=')]
    assert cookies
    assert 'Path=;' not in cookies[0] and not cookies[0].rstrip().endswith(
        'Path=')


def _code_for_path_test():
    _emp('SGPATH')
    return 'SGPATH'
