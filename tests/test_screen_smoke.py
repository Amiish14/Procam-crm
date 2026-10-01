"""scripts/screen_smoke.py — an administrator opening every main screen
as somebody else, without a browser and without a password.

Two things have to hold or the script is worse than useless: a broken
screen must be reported as broken (and a correctly refused one must not),
and running it must be incapable of changing a single row.
"""
import json
import os
import sys
import tempfile
import time

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ['TALISMAN_FORCE_HTTPS'] = 'false'
os.environ.setdefault('SECRET_KEY', 'screen-smoke-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'ScreenSmokeTest12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'screensmoke.db'))

import importlib                                            # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Employee = _main.Employee

# Appended after the app import: scripts/ holds an email_ingest.py that
# would otherwise shadow the package of the same name.
sys.path.append(os.path.join(_ROOT, 'scripts'))
import screen_smoke as smoke                                # noqa: E402

from app.models.access import AccessProfile, DataScope      # noqa: E402

REP = 'SMKREP'
SOURCE = open(os.path.join(_ROOT, 'scripts', 'screen_smoke.py')).read()


@pytest.fixture()
def rep():
    """One ordinary salesperson: own records only, no special grants."""
    with flask_app.app_context():
        db.create_all()
        e = (Employee.query.filter_by(emp_code=REP).first()
             or Employee(emp_code=REP))
        e.name, e.role, e.vertical = 'Smoke Rep', 'user', 'Project Freight'
        e.is_active, e.must_change_pw = True, False
        e.is_super_admin, e.is_vertical_head = False, False
        e.session_version = 0
        db.session.add(e)
        AccessProfile.query.filter_by(emp_code=REP).delete()
        db.session.add(AccessProfile(emp_code=REP, data_scope=DataScope.OWN,
                                     perms=[]))
        db.session.commit()
        return REP


# ── a fabricated answer, so the rules can be exercised exactly ───────
class _Resp:
    def __init__(self, status, body=b'body', headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body

    def get_data(self):
        return self._body


class _Stub:
    """A client that answers with whatever the test asked for."""

    def __init__(self, status, body=b'body', headers=None, delay=0.0):
        self.status, self.body, self.headers, self.delay = (
            status, body, headers, delay)
        self.paths = []

    def get(self, path):
        self.paths.append(path)
        if self.delay:
            time.sleep(self.delay)
        return _Resp(self.status, self.body, self.headers)


# ── verdicts ─────────────────────────────────────────────────────────
def test_a_500_is_a_failure():
    row = smoke.check(_Stub(500, b'oops'), '/hygiene', 'hygiene')
    assert row['verdict'] == smoke.FAIL
    assert row['status'] == 500 and 'server error' in row['note']
    assert smoke.failed([row])


def test_a_403_is_refused_not_a_failure():
    row = smoke.check(_Stub(403, b'no'), '/management', 'management')
    assert row['verdict'] == smoke.REFUSED
    assert row['note'] == 'refused (correct for this scope)'
    assert not smoke.failed([row])
    assert smoke.counts([row])[smoke.REFUSED] == 1


def test_a_lost_session_is_a_failure_not_a_redirect_to_report():
    """A 302 to the sign-in page makes every later verdict meaningless,
    so it must not be reported as a mild warning."""
    row = smoke.check(_Stub(302, b'', {'Location': '/login'}), '/app', '')
    assert row['verdict'] == smoke.FAIL
    assert smoke.verdict(401, 10)[0] == smoke.FAIL
    assert smoke.verdict(404, 10)[0] == smoke.FAIL
    assert smoke.verdict(302, 10, '/companies')[0] == smoke.WARN


def test_a_slow_page_is_a_warning_not_a_failure():
    assert smoke.verdict(200, 2001, slow_ms=2000)[0] == smoke.WARN
    assert smoke.verdict(200, 2000, slow_ms=2000)[0] == smoke.OK
    row = smoke.check(_Stub(200, delay=0.02), '/global-crm', '', slow_ms=0)
    assert row['verdict'] == smoke.WARN and 'slow' in row['note']
    assert not smoke.failed([row])


def test_a_timing_and_a_size_are_recorded_for_every_screen():
    stub = _Stub(200, b'x' * 4096, delay=0.01)
    rows = smoke.run(stub, (('/a', 'a'), ('/b', 'b')))
    assert [r['path'] for r in rows] == ['/a', '/b']
    assert all(r['ms'] >= 10 for r in rows), rows
    assert all(r['bytes'] == 4096 for r in rows)
    assert all(r['verdict'] == smoke.OK for r in rows)


def test_exit_status_is_one_only_when_something_failed(rep, monkeypatch):
    def answer(verdicts):
        return lambda *a, **kw: [
            {'path': '/x', 'label': '', 'status': 200, 'ms': 1, 'bytes': 1,
             'verdict': v, 'note': ''} for v in verdicts]

    monkeypatch.setattr(smoke, 'run', answer([smoke.OK, smoke.WARN,
                                              smoke.REFUSED]))
    assert smoke.main(['--as', rep]) == 0
    monkeypatch.setattr(smoke, 'run', answer([smoke.OK, smoke.FAIL]))
    assert smoke.main(['--as', rep]) == 1


def test_an_unknown_or_inactive_employee_is_an_argument_error(rep):
    assert smoke.main(['--as', 'NOSUCHCODE']) == 2
    with flask_app.app_context():
        emp = Employee.query.filter_by(emp_code=rep).first()
        emp.is_active = False
        db.session.commit()
    try:
        assert smoke.main(['--as', rep]) == 2
    finally:
        with flask_app.app_context():
            emp = Employee.query.filter_by(emp_code=rep).first()
            emp.is_active = True
            db.session.commit()


def test_the_json_report_is_machine_readable(rep, tmp_path, monkeypatch):
    out = tmp_path / 'smoke.json'
    monkeypatch.setattr(smoke, 'run', lambda *a, **kw: [
        {'path': '/app', 'label': 'shell', 'status': 200, 'ms': 12,
         'bytes': 100, 'verdict': smoke.OK, 'note': ''}])
    assert smoke.main(['--as', rep, '--json', str(out)]) == 0
    data = json.loads(out.read_text())
    assert data['as']['emp_code'] == rep
    assert data['as']['data_scope'] == DataScope.OWN
    assert data['counts'][smoke.OK] == 1 and data['failed'] is False
    assert data['results'][0]['ms'] == 12


# ── read-only, enforced rather than promised ─────────────────────────
def test_the_script_contains_no_writing_verb_at_all():
    """Textual, because the guarantee is about the whole file and not
    only the paths a test happens to walk."""
    for forbidden in ('.post(', '.put(', '.patch(', '.delete(', '.head(',
                      'method=', 'methods='):
        assert forbidden not in SOURCE, forbidden
    # Exactly one call is ever made on the wrapped client, and it is get.
    assert SOURCE.count('self._client.') == 1
    assert 'self._client.get(' in SOURCE


def test_the_read_only_client_has_no_verb_but_get(rep):
    with flask_app.app_context():
        emp = Employee.query.filter_by(emp_code=rep).first()
        client = smoke.sign_in(flask_app, emp)
    for verb in ('post', 'put', 'patch', 'delete', 'open'):
        with pytest.raises(AttributeError):
            getattr(client, verb)


def test_the_run_issues_nothing_but_get_requests(rep, monkeypatch):
    """Proof by observation as well as by reading: every request the run
    makes is recorded as it leaves the client."""
    seen = []
    with flask_app.app_context():
        emp = Employee.query.filter_by(emp_code=rep).first()
        client = smoke.sign_in(flask_app, emp)
        inner = object.__getattribute__(client, '_client')
        real_open = inner.open

        def watch(*args, **kwargs):
            seen.append(kwargs.get('method', 'GET'))
            return real_open(*args, **kwargs)

        monkeypatch.setattr(inner, 'open', watch)
        for verb in ('post', 'put', 'patch', 'delete'):
            monkeypatch.setattr(inner, verb, _refuse(verb))
        with smoke.configured(flask_app):
            rows = smoke.run(client, smoke.SCREENS[:4])

    assert len(rows) == 4
    assert seen and set(seen) == {'GET'}
    assert client.calls == [p for p, _l in smoke.SCREENS[:4]]


def _refuse(verb):
    def boom(*a, **kw):
        raise AssertionError(f'screen_smoke issued a {verb.upper()}')
    return boom


# ── against the real application ─────────────────────────────────────
def test_every_named_screen_is_a_real_route():
    """A typo in the list would be reported as a broken screen forever."""
    rules = {str(r) for r in flask_app.url_map.iter_rules()}
    missing = [p for p, _l in smoke.SCREENS if p not in rules]
    assert not missing, missing


def test_a_real_run_answers_for_every_screen_and_refuses_the_right_ones(rep):
    with flask_app.app_context():
        emp = Employee.query.filter_by(emp_code=rep).first()
        with smoke.configured(flask_app):
            client = smoke.sign_in(flask_app, emp)
            rows = smoke.run(client, smoke.SCREENS)

    assert len(rows) == len(smoke.SCREENS)
    for r in rows:
        assert r['verdict'] in (smoke.OK, smoke.WARN, smoke.REFUSED,
                                smoke.FAIL)
        assert r['ms'] >= 0 and r['bytes'] >= 0
        # Nothing may be reported as signed out: the session must take.
        assert r['status'] != 401, r
        assert 'did not take' not in r['note'], r

    by_path = {r['path']: r for r in rows}
    # A rep looks after nobody, so the management view is refused — and
    # that is the screen working, not the screen failing.
    assert by_path['/management']['verdict'] == smoke.REFUSED
    assert by_path['/my-work']['status'] == 200
    assert by_path['/api/workbench']['status'] == 200


def test_the_report_names_the_viewer_and_says_it_was_read_only(rep):
    rows = [{'path': '/app', 'label': 'shell', 'status': 200, 'ms': 5,
             'bytes': 2048, 'verdict': smoke.OK, 'note': ''}]
    with flask_app.app_context():
        emp = Employee.query.filter_by(emp_code=rep).first()
        text = smoke.report(rows, smoke._who(emp))
    assert REP in text and 'GET only' in text
    assert '2.0 kB' in text and '1 ok' in text
