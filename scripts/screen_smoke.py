#!/usr/bin/env python3
"""
Open every main screen as one employee and report what happens.
Read-only: this script issues GET requests and nothing else, ever.

    .venv/bin/python scripts/screen_smoke.py --as EMP372011
    .venv/bin/python scripts/screen_smoke.py --as EMP372011 \
        --json /tmp/smoke.json
    .venv/bin/python scripts/screen_smoke.py --as EMP372011 --only review

Why it exists
    After a release somebody has to confirm the new screens actually open
    for the people who will use them, and that a rep is still refused the
    things a rep should be refused. Doing that in a browser means knowing
    somebody's password and clicking fifteen links. This does it from the
    server, in-process, in a few seconds.

How it signs in
    Through the Flask test client, by writing the session the login route
    would have written — the same thing the test suite does. No password
    is needed, no network call is made, nothing is written to the
    employee's record, and no real session is created for them.

How read-only is enforced, not merely promised
    The test client is wrapped in `ReadOnlyClient`, which forwards `get`
    and has no other verb on it at all. Reaching for any writing verb on
    it raises AttributeError instead. A promise in a docstring is worth
    less than an object that cannot do the thing, and the test suite
    checks both the object and the text of this file.

Verdicts
    OK        the screen answered, within the slow threshold
    REFUSED   403 — the Access Matrix turned this person away, which is
              the screen working, not failing
    WARN      slow (over --slow-ms, 2000 by default), or an unexpected
              redirect
    FAIL      500, a missing route, a lost session, or an exception

Exit status is 1 when anything FAILed and 0 otherwise, so it can gate a
deploy script. WARN and REFUSED do not fail the run. Argument errors
exit 2.

One honest caveat: opening the screens means importing the application,
which runs the ordinary boot — the same boot gunicorn runs, including
its autoheal of a missing table. That is the application starting, not
this script writing; nothing here issues anything but GET.
"""
import argparse
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The test client speaks plain http at the application root. Three
# settings would otherwise turn every page into a redirect and make the
# whole run look broken:
#   URL_PREFIX           the live server is behind nginx at /CRM; the
#                        routes themselves are registered without it
#   SESSION_COOKIE_SECURE a Secure-only cookie is dropped over http, so
#                        every page would read as signed out
#   TALISMAN_FORCE_HTTPS  would 302 every request to https
os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ['TALISMAN_FORCE_HTTPS'] = 'false'

from app import app as flask_app, Employee            # noqa: E402

OK, WARN, FAIL, REFUSED = 'OK', 'WARN', 'FAIL', 'REFUSED'

#: Over this, a screen is a WARN. Two seconds is the point at which a
#: daily screen stops feeling like a tool and starts feeling like a wait.
SLOW_MS = 2000

#: The screens an administrator is checking after a release, in the
#: order somebody would walk them. Pages first, then the JSON endpoints
#: the pages are built on — when a page is blank, the endpoint below it
#: says whether the data or the markup is at fault.
SCREENS = (
    ('/app',                      'the app shell'),
    ('/my-work',                  'Daily Workbench'),
    ('/my-work/tasks',            'my tasks'),
    ('/team-workbench',           'Team Workbench'),
    ('/review/individual',        'individual review'),
    ('/review/vertical',          'vertical review'),
    ('/review/meeting',           'meeting review'),
    ('/management',               'management command view'),
    ('/global-crm',               'Global CRM'),
    ('/hygiene',                  'CRM Hygiene Score'),
    ('/intelligence/projects',    'project intelligence'),
    ('/intelligence/competitors', 'competitor intelligence'),
    ('/intelligence/vendors',     'vendor intelligence'),
    ('/admin/data-quality',       'Data Quality'),
    ('/notifications',            'notifications'),
    ('/api/workbench',            'workbench API'),
    ('/api/hygiene',              'hygiene API'),
    ('/api/directory/dashboard',  'directory API'),
)


class ReadOnlyClient:
    """A Flask test client with every verb but GET taken off it.

    Deliberately not a subclass: inheriting would carry `post`, `put`,
    `delete` and `open` along with it, and this script's one guarantee is
    that those are unreachable from here.
    """

    __slots__ = ('_client', 'calls')

    def __init__(self, client):
        self._client = client
        #: Every path asked for, in order — the run's own record of what
        #: it touched.
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        # follow_redirects=False on purpose: a redirect is a result worth
        # reporting, not something to chase quietly.
        return self._client.get(path, follow_redirects=False)

    def __getattr__(self, name):
        raise AttributeError(
            f'screen_smoke is read-only: this client has no {name!r}, '
            f'only get()')


@contextmanager
def configured(app):
    """Ask Flask for a real 500 page instead of a raised exception.

    A smoke test has to report a broken screen and carry on to the next
    one. With TESTING or PROPAGATE_EXCEPTIONS on, the first broken screen
    ends the run. Restored afterwards so importing this module from the
    test suite does not change the suite's app.
    """
    before = (app.config.get('TESTING'),
              app.config.get('PROPAGATE_EXCEPTIONS'))
    app.config['TESTING'] = False
    app.config['PROPAGATE_EXCEPTIONS'] = False
    try:
        yield app
    finally:
        app.config['TESTING'], app.config['PROPAGATE_EXCEPTIONS'] = before


def sign_in(app, emp):
    """Write the session the login route would have written.

    `sv` matters: the session guard ends any session issued under an
    older session_version, and without it every page answers with a
    redirect to /login. `must_change_pw` is deliberately left unset —
    it is about choosing a password, and setting it would make every
    JSON endpoint answer 403 and tell us nothing about the screens.
    """
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['emp_code'] = emp.emp_code
        sess['name'] = emp.name
        sess['role'] = emp.role
        sess['vertical'] = emp.vertical or ''
        sess['sv'] = emp.session_version or 0
    return ReadOnlyClient(client)


def verdict(status, ms, location='', *, slow_ms=SLOW_MS):
    """(verdict, note) for one answered request.

    Pure, so the rules can be read and tested without a web server.
    """
    if status >= 500:
        return FAIL, 'server error'
    if status == 404:
        return FAIL, 'no such route — the screen is not registered'
    if status == 403:
        return REFUSED, 'refused (correct for this scope)'
    if status == 401:
        return FAIL, 'not authenticated — the session did not take'
    if status in (301, 302, 303, 307, 308):
        # A redirect to the sign-in page means the session was rejected,
        # so every verdict after it would be meaningless. Any other
        # redirect is merely worth seeing.
        if '/login' in (location or ''):
            return FAIL, 'redirected to sign-in — the session did not take'
        return WARN, f'redirected to {location or "?"}'
    if ms > slow_ms:
        return WARN, f'slow — {ms} ms, over {slow_ms} ms'
    return OK, ''


def check(client, path, label='', *, slow_ms=SLOW_MS):
    """GET one screen and judge the answer."""
    start = time.perf_counter()
    try:
        resp = client.get(path)
    except Exception as exc:                       # pragma: no cover - rare
        ms = int(round((time.perf_counter() - start) * 1000))
        return {'path': path, 'label': label, 'status': 0, 'ms': ms,
                'bytes': 0, 'verdict': FAIL,
                'note': f'{type(exc).__name__}: {exc}'[:200]}
    ms = int(round((time.perf_counter() - start) * 1000))
    body = resp.get_data()
    location = resp.headers.get('Location', '') if hasattr(
        resp, 'headers') else ''
    mark, note = verdict(resp.status_code, ms, location, slow_ms=slow_ms)
    return {'path': path, 'label': label, 'status': resp.status_code,
            'ms': ms, 'bytes': len(body), 'verdict': mark, 'note': note}


def run(client, screens=SCREENS, *, slow_ms=SLOW_MS):
    return [check(client, path, label, slow_ms=slow_ms)
            for path, label in screens]


def counts(results):
    return {mark: sum(1 for r in results if r['verdict'] == mark)
            for mark in (OK, WARN, REFUSED, FAIL)}


def failed(results):
    return any(r['verdict'] == FAIL for r in results)


# ── reporting ────────────────────────────────────────────────────────
def _size(n):
    return f'{n / 1024:.1f} kB' if n >= 1024 else f'{n} B'


def report(results, who, *, slow_ms=SLOW_MS):
    out = [f'\n  Procam CRM screen smoke — {datetime.now():%Y-%m-%d %H:%M}',
           f'  as {who["emp_code"]} · {who["name"]} · role={who["role"]} '
           f'· data scope={who["data_scope"]}'
           + ('  (super admin)' if who.get('is_super_admin') else ''),
           '  GET only — this run cannot have changed anything.\n']
    for r in results:
        out.append(f'    {r["verdict"]:<8}{r["status"]:>4}  {r["ms"]:>6} ms  '
                   f'{_size(r["bytes"]):>9}  {r["path"]:<28}'
                   f'{r["note"] or r["label"]}')
    c = counts(results)
    out.append(f'\n  {c[OK]} ok · {c[WARN]} warn · {c[REFUSED]} refused · '
               f'{c[FAIL]} fail   (slow is over {slow_ms} ms)')
    return '\n'.join(out)


def _who(emp):
    from app.access.service import effective

    data_scope, perms = effective(emp.emp_code)
    return {'emp_code': emp.emp_code, 'name': emp.name or '',
            'role': emp.role or '', 'vertical': emp.vertical or '',
            'data_scope': data_scope,
            'is_super_admin': bool(emp.is_super_admin),
            'is_vertical_head': bool(emp.is_vertical_head),
            'perms': sorted(perms)}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Open every main screen as one employee (read-only, '
                    'GET requests only, in-process — no network).')
    ap.add_argument('--as', dest='emp_code', required=True,
                    help='emp_code to look as, e.g. EMP372011')
    ap.add_argument('--json', dest='json_path', metavar='PATH',
                    help='also write the report here as JSON')
    ap.add_argument('--slow-ms', type=int, default=SLOW_MS,
                    help=f'a page slower than this is a WARN '
                         f'(default {SLOW_MS})')
    ap.add_argument('--only', default='',
                    help='comma-separated substrings; check only the '
                         'screens whose path contains one of them')
    args = ap.parse_args(argv)

    wanted = [s.strip() for s in args.only.split(',') if s.strip()]
    screens = [(p, l) for p, l in SCREENS
               if not wanted or any(w in p for w in wanted)]
    if not screens:
        ap.error('--only matched no screens')

    with flask_app.app_context():
        emp = Employee.query.filter_by(
            emp_code=args.emp_code.strip().upper()).first()
        if emp is None:
            print(f'screen_smoke: no employee {args.emp_code!r}',
                  file=sys.stderr)
            return 2
        if not emp.is_active:
            print(f'screen_smoke: {emp.emp_code} is not an active employee '
                  f'— their session would be ended on the first request',
                  file=sys.stderr)
            return 2
        who = _who(emp)
        with configured(flask_app):
            client = sign_in(flask_app, emp)
            results = run(client, screens, slow_ms=args.slow_ms)

    print(report(results, who, slow_ms=args.slow_ms))
    if args.json_path:
        payload = {'generated_at': datetime.now().isoformat(
                       timespec='seconds'),
                   'as': who, 'slow_ms': args.slow_ms, 'results': results,
                   'counts': counts(results), 'failed': failed(results)}
        with open(args.json_path, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, indent=1, default=str)
        print(f'  written to {args.json_path}')
    return 1 if failed(results) else 0


if __name__ == '__main__':
    raise SystemExit(main())
