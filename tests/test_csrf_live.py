"""
Every write endpoint the templates call, posted to with CSRF ON.

The companion to `test_csrf_write_paths.py`. That one reads the
templates and checks they *send* a token; this one runs the real app
with `CSRFProtect` armed and checks the server *accepts* one — and,
first, that it rejects a write without one, because a test that
cannot fail proves nothing.

Why this did not exist. Every other test module sets
`WTF_CSRF_ENABLED = False`, which is the right trade for testing
behaviour and makes all of them structurally blind to a missing
token. Five dead buttons shipped under a green suite before anyone
noticed; six more were already in production. So this module pays
the cost once: CSRF stays on, every request carries a token, and the
endpoints the pages actually call are exercised.

What it asserts, and deliberately not more. A write must not come
back as a **CSRF rejection** — `{'code': 'csrf'}`, HTTP 400. It may
come back 400 for a bad payload, 403 for permission, 404 for a
missing record, even 500: this module is not testing those
endpoints' logic, only that the gate in front of them opens. The
distinction matters, because a test that demanded 200 from eighty
endpoints would need eighty valid payloads and would be abandoned
within a month.
"""
import glob
import os
import re
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'csrf-live-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'CsrfLiveTest12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'csrflive.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Employee = _main.Employee


from flask import session as flask_session             # noqa: E402
from flask_wtf.csrf import generate_csrf               # noqa: E402

from app.models.access import AccessProfile, DataScope  # noqa: E402

_TEMPLATES = os.path.join(_ROOT, 'templates')
ADMIN = 'CSRFADM'


#: There is no exclusion list, and there was one until the sentinel
#: existed. Posting to `/api/bulk/leads/delete` or the mail sender
#: needed real care while the requests reached their views; now the
#: `armed` fixture stops every one of them at the gate, so the
#: dangerous endpoints are exactly as safe as the harmless ones and
#: get the same coverage. If the sentinel ever fails to register, the
#: 299 assertion fails loudly rather than letting a bulk delete run.


# ── finding the endpoints ────────────────────────────────────────────
#: Pages write either with a bare `fetch` or through a small helper
#: defined on the page. Both have to be seen, or the scan reports
#: confident coverage of a third of the surface.
_CALLERS = ('fetch', 'jf', 'jfetch', 'jsonFetch', 'api',
            'postJson', 'writeJson', 'apiPost', 'post')
_CALL = re.compile(
    r'(?<![\w.])(?P<fn>' + '|'.join(_CALLERS) + r')\s*\(\s*'
    r'(?P<url>`[^`]*`|\'[^\']*\'|"[^"]*")(?P<rest>.{0,400})', re.S)
_METHOD = re.compile(r"""method\s*:\s*['"`]?(POST|PUT|PATCH|DELETE)""", re.I)

#: Helpers whose verb is in their name rather than their arguments.
_IMPLIED = {'postJson': 'POST', 'apiPost': 'POST', 'writeJson': 'POST',
            'post': 'POST'}


def _literal_path(raw):
    """A JS url expression as a concrete path, or None.

    Interpolated segments become `1`, which satisfies the `<int:…>`
    converters these routes use. A path that still has Jinja or JS in
    it after that is not resolvable and is skipped.
    """
    url = raw.strip('`\'"')
    url = url.replace('{{ url_prefix }}', '').replace("{{ url_prefix|tojson }}", '')
    url = re.sub(r'\{\{[^}]*\}\}', '1', url)        # Jinja: {{ h.id }}
    url = re.sub(r'\$\{[^}]*\}', '1', url)          # `...${id}...`
    url = re.sub(r"'\s*\+\s*[^+]+\+\s*'", '1', url)  # '...' + id + '...'
    url = re.sub(r"'\s*\+\s*[A-Za-z_][\w.$\[\]]*", '1', url)
    url = url.split('?')[0].strip()
    if not url.startswith('/'):
        return None
    # `fetch('/api/leads/' + id, …)`: the id is outside the string
    # literal, so what is captured ends in a slash and matches no
    # route. Standing in `1` for the missing segment recovers about
    # thirty endpoints that would otherwise look like no-coverage.
    if url.endswith('/') and len(url) > 1:
        url += '1'
    if '{' in url or '}' in url or '$' in url or '+' in url:
        return None
    return url


def discovered():
    """[(template, path, method)] for every write a page makes."""
    found, seen = [], set()
    for path in sorted(glob.glob(os.path.join(_TEMPLATES, '**', '*.html'),
                                 recursive=True)):
        with open(path, encoding='utf-8', errors='ignore') as fh:
            src = fh.read()
        for m in _CALL.finditer(src):
            verb = _METHOD.search(m.group('rest'))
            method = (verb.group(1).upper() if verb
                      else _IMPLIED.get(m.group('fn')))
            if not method:
                continue
            url = _literal_path(m.group('url'))
            if not url:
                continue
            key = (url, method)
            if key in seen:
                continue
            seen.add(key)
            found.append((os.path.relpath(path, _ROOT),) + key)
    return found


def _routable(url, method):
    """Whether the app actually has this route, so a 404 from a typo
    in the scan is not mistaken for a tested endpoint."""
    adapter = flask_app.url_map.bind('localhost')
    try:
        adapter.match(url, method=method)
        return True
    except Exception:                                     # noqa: BLE001
        return False


TARGETS = [t for t in discovered() if _routable(t[1], t[2])]


# ── arming CSRF, and stopping at the gate ────────────────────────────
@pytest.fixture()
def armed():
    """CSRF on for the duration of one test, and the view unreachable.

    Two problems this solves, both caused by every module sharing one
    Flask app and one database.

    The flag cannot be set at import. Modules are imported in
    alphabetical order and most of them set `WTF_CSRF_ENABLED = False`
    on the same app object, so a value set here at import time is
    gone by the time these tests run — which is how this module's own
    "is CSRF on?" check failed in the full suite while passing alone.

    And the requests must not reach the views. Posting `{}` to fifty
    endpoints against a shared database creates records that other
    modules then find: the first full run of this file left ten news
    items behind and failed a test in `test_intel.py` that asserts an
    empty table. So a `before_request` registered *after*
    CSRFProtect's own stops every request the moment the token has
    been checked. The gate is exercised; nothing behind it runs.
    """
    cfg = flask_app.config
    before = (cfg.get('WTF_CSRF_ENABLED'), cfg.get('WTF_CSRF_SSL_STRICT'))
    cfg['WTF_CSRF_ENABLED'] = True
    cfg['WTF_CSRF_SSL_STRICT'] = False       # the test client is http

    from flask import Response

    def _stop_at_the_gate():
        return Response(GATE_PASSED, status=299)

    funcs = flask_app.before_request_funcs.setdefault(None, [])
    funcs.append(_stop_at_the_gate)
    try:
        yield
    finally:
        funcs.remove(_stop_at_the_gate)
        cfg['WTF_CSRF_ENABLED'], cfg['WTF_CSRF_SSL_STRICT'] = before


#: What the sentinel returns when CSRF has let a request through.
GATE_PASSED = 'csrf-gate-passed'


# ── the client ───────────────────────────────────────────────────────
@pytest.fixture(scope='module')
def admin():
    with flask_app.app_context():
        db.create_all()
        e = (Employee.query.filter_by(emp_code=ADMIN).first()
             or Employee(emp_code=ADMIN))
        e.name, e.is_active, e.must_change_pw = 'CSRF Admin', True, False
        e.role, e.is_super_admin, e.session_version = 'admin', True, 0
        e.email = 'csrfadm@procamgroup.in'
        db.session.add(e)
        db.session.flush()
        AccessProfile.query.filter_by(emp_code=ADMIN).delete()
        db.session.add(AccessProfile(emp_code=ADMIN,
                                     data_scope=DataScope.ALL, perms=[]))
        db.session.commit()
    return ADMIN


def _client_and_token(code):
    """A signed-in client and a token its session will accept."""
    client = flask_app.test_client()
    with flask_app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as s:
        s['csrf_token'] = raw
        s.update(emp_code=code, name=code, role='admin', sv=0)
    return client, token


def _send(client, url, method, token=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['X-CSRFToken'] = token
    return client.open(url, method=method, headers=headers, json={})


def _is_csrf_rejection(resp):
    if resp.status_code != 400:
        return False
    body = resp.get_json(silent=True) or {}
    if body.get('code') == 'csrf':
        return True
    return b'Session expired' in (resp.data or b'')


# ── the tests ────────────────────────────────────────────────────────
def test_csrf_is_actually_on_in_this_module(admin, armed):
    """Without this, every assertion below passes vacuously — which
    is exactly how the bug survived five times."""
    assert flask_app.config['WTF_CSRF_ENABLED'] is True
    client, _ = _client_and_token(admin)
    resp = _send(client, '/api/tasks/1/done', 'POST')      # no token
    assert _is_csrf_rejection(resp), (
        f'a tokenless write was not rejected (got {resp.status_code}); '
        f'CSRF is not armed and this module proves nothing')


def test_a_tokened_write_reaches_the_gate_and_stops(admin, armed):
    """The other half: the sentinel proves the request got past CSRF
    and that nothing behind it ran."""
    client, token = _client_and_token(admin)
    resp = _send(client, '/api/tasks/1/done', 'POST', token=token)
    assert resp.status_code == 299
    assert GATE_PASSED in resp.get_data(as_text=True)


def test_the_scan_found_a_sensible_number_of_endpoints():
    """A silent zero would make the suite below green and empty.

    52 on 19 October. The floor is set below that rather than at it,
    so deleting a page does not fail the build — but a scanner that
    quietly stops matching, which is the way this kind of test dies,
    does.
    """
    assert len(TARGETS) >= 45, (
        f'only {len(TARGETS)} write endpoints discovered — the scanner '
        f'has probably stopped matching the templates')


@pytest.mark.parametrize(
    'template,url,method',
    TARGETS,
    ids=[f'{t[2]} {t[1]}' for t in TARGETS])
def test_a_tokened_write_is_not_refused_for_csrf(admin, armed, template,
                                                 url, method):
    """The gate opens. What is behind it is somebody else's test —
    and it does not run: see the `armed` fixture."""
    client, token = _client_and_token(admin)
    resp = _send(client, url, method, token=token)
    assert not _is_csrf_rejection(resp), (
        f'{template} calls {method} {url}, and it is refused for CSRF '
        f'even with a token: {(resp.get_json(silent=True) or {})}')
    assert resp.status_code == 299, (
        f'{method} {url} did not reach the sentinel — something before '
        f'the view refused it with {resp.status_code}')


def test_the_pages_fixed_today_are_in_the_scan():
    """Named, so a change that stops the scanner seeing them shows up
    here rather than as quietly reduced coverage."""
    seen = {t[0] for t in discovered()}
    for name in ('templates/help/admin.html',
                 'templates/company/list.html',
                 'templates/company/detail.html',
                 'templates/business_card/scan.html',
                 'templates/bulk_admin/leads.html',
                 'templates/workbench/home.html',
                 'templates/competitor/list.html'):
        assert name in seen, f'{name} writes, and the scan missed it'
