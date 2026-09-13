"""
/admin/ops and /api/ops/status: administrators only, the stored report
shown with its age and escaped, and the live section limited to what a
web worker can do without a network call or a system command.
"""
import json
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'OpsRoutesTestOnly12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'ops.db'))

from app import app as flask_app, db, Employee                   # noqa: E402
from app.ops import checks, routes as ops_routes                 # noqa: E402

flask_app.config['WTF_CSRF_ENABLED'] = False

if 'ops' not in flask_app.blueprints:
    # app.py's blueprint list registers this in the running app. Here the
    # shared test app may already have served a request, after which Flask
    # refuses a late registration, so the guard is lifted for this one call.
    _served = flask_app._got_first_request
    flask_app._got_first_request = False
    try:
        flask_app.register_blueprint(ops_routes.bp)
    finally:
        flask_app._got_first_request = _served


@pytest.fixture()
def people():
    with flask_app.app_context():
        db.create_all()
        made = []
        for code, role in (('OPSADM', 'admin'), ('OPSREP', 'user')):
            e = Employee.query.filter_by(emp_code=code).first()
            if e is None:
                e = Employee(emp_code=code, name=code)
                db.session.add(e)
                made.append(code)
            e.role, e.is_active, e.must_change_pw = role, True, False
            e.vertical, e.is_super_admin = 'All', False
            e.session_version, e.failed_logins, e.locked_until = 0, 0, None
            e.set_password('OpsRoutesPassword-1')
        db.session.commit()
        yield
        for code in made:
            Employee.query.filter_by(emp_code=code).delete()
        db.session.commit()


@pytest.fixture()
def status_file(tmp_path, monkeypatch):
    path = tmp_path / 'ops_status.json'
    monkeypatch.setenv('OPS_STATUS_FILE', str(path))
    return path


def _client(code=None, role=None):
    c = flask_app.test_client()
    if code:
        with c.session_transaction() as s:
            s.update(emp_code=code, name=code, role=role, vertical='All')
    return c


def _write(path, detail='fine', status='OK', age_s=0):
    ctx = checks.Context(network=False, schema=False)
    report = checks.build_report(
        [checks.result('backups', 'Newest backup', status, detail)], ctx)
    checks.write_status(report, str(path))
    t = time.time() - age_s
    os.utime(path, (t, t))


def test_only_administrators_get_in(people, status_file):
    assert _client().get('/api/ops/status').status_code == 401
    assert _client().get('/admin/ops').status_code == 302
    rep = _client('OPSREP', 'user')
    assert rep.get('/api/ops/status').status_code == 403
    assert rep.get('/admin/ops').status_code == 403
    assert _client('OPSADM', 'admin').get('/admin/ops').status_code == 200


def test_the_page_shows_the_stored_report_escaped(people, status_file):
    _write(status_file, detail='<script>alert(1)</script> backup late',
           status='WARN')
    r = _client('OPSADM', 'admin').get('/admin/ops')
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;alert(1)&lt;/script&gt; backup late' in html
    assert 'Newest backup' in html and 'Live from this page load' in html
    assert 'older than' not in html                     # fresh


def test_a_stale_or_missing_report_is_called_out(people, status_file):
    r = _client('OPSADM', 'admin').get('/admin/ops')
    assert 'install' in r.get_data(as_text=True)
    _write(status_file, age_s=3 * 3600)
    html = _client('OPSADM', 'admin').get('/admin/ops').get_data(as_text=True)
    assert 'older than 30 minutes' in html


def test_the_api_returns_stored_and_live(people, status_file, monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError('the web process must not run this')
    monkeypatch.setattr(checks, '_run', forbidden)
    monkeypatch.setattr(checks, '_http', forbidden)
    monkeypatch.setattr(checks, '_peer_cert', forbidden)
    _write(status_file, age_s=120)
    r = _client('OPSADM', 'admin').get('/api/ops/status')
    assert r.status_code == 200
    d = r.get_json()
    assert d['ok'] is True and d['stored']['stale'] is False
    assert 115 <= d['stored']['age_seconds'] <= 200
    assert d['stored']['report']['checks'][0]['key'] == 'backups'
    keys = [c['key'] for c in d['live']]
    assert keys == ['database_stats', 'email_queue', 'review_queue',
                    'copilot_index']
    for c in d['live']:
        assert 'crashed' not in c['detail'], c
    assert d['live'][0]['status'] == checks.OK      # the test database
    assert d['live_overall'] in (checks.OK, checks.WARN, checks.UNKNOWN)
    assert os.environ['ADMIN_INITIAL_PASSWORD'] not in json.dumps(d)
