"""The main app page must actually contain the app.

A CSS rule reading `@media(...){#crm-fab{...}}` once opened a Jinja comment
(`{` immediately followed by `#`), which swallowed 47KB of the template up
to the next `#}` — taking the shell, the sidebar and every pipeline page
with it.  The route returned 200 the whole time, so a status-code smoke
test could not see it.  Users got a blank white screen.

These tests assert on content, not status.
"""
import os
import re
import sys
import glob
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'ShellTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'shell.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db, Employee = _main.app, _main.db, _main.Employee

_TEMPLATES = os.path.join(_ROOT, 'templates')

# Every top-level page the single-page app switches between.
REQUIRED_IDS = [
    'shell', 'topnav', 'pg-dash', 'pg-pipeline', 'pg-leads',
    'pg-contacts', 'pg-outreach', 'pg-opp', 'pg-team',
    'pg-employees', 'pg-access', 'pg-upload', 'pg-pwchange',
]
# pg-accounts and pg-projects are created at runtime by ensurePreSalesPages(),
# so they are deliberately not expected in the server-rendered HTML.


@pytest.fixture(scope='module')
def admin_html():
    with flask_app.app_context():
        db.create_all()
        e = Employee.query.filter_by(emp_code='SHELLADM').first()
        if not e:
            e = Employee(emp_code='SHELLADM', name='Shell Admin')
            db.session.add(e)
        e.role, e.is_active, e.must_change_pw = 'admin', True, False
        e.vertical = 'All'
        db.session.commit()

    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='SHELLADM', name='Shell Admin', role='admin',
                 vertical='All')
    r = c.get('/app')
    assert r.status_code == 200
    return r.get_data(as_text=True)


@pytest.mark.parametrize('element_id', REQUIRED_IDS)
def test_page_is_present(admin_html, element_id):
    assert f'id="{element_id}"' in admin_html or \
           f'class="shell"' in admin_html and element_id == 'shell', \
        f'{element_id} missing from the rendered page'


def test_nothing_was_swallowed(admin_html):
    """Rendering only strips comments and fills variables.

    A large shortfall means a chunk of the template vanished, which is what
    an accidental Jinja comment does.
    """
    tpl = open(os.path.join(_TEMPLATES, 'app.html')).read()
    lost = len(tpl) - len(admin_html)
    assert lost < 2000, (
        f'{lost} characters missing from the rendered page — a Jinja '
        f'construct is probably swallowing part of the template')


def test_no_accidental_jinja_comment_openers():
    """`{` followed by `#` inside CSS or JS silently eats the template."""
    opener = '{' + '#'
    offenders = []
    for path in glob.glob(os.path.join(_TEMPLATES, '**', '*.html'),
                          recursive=True):
        for n, line in enumerate(open(path), 1):
            if opener not in line:
                continue
            # A real Jinja comment closes on the same line, or the line is
            # a deliberate one-line comment.
            if ('#' + '}') in line:
                continue
            offenders.append(f'{os.path.relpath(path, _ROOT)}:{n}: '
                             f'{line.strip()[:90]}')
    assert not offenders, (
        'Unclosed Jinja comment opener — put a space between the brace and '
        'the hash:\n  ' + '\n  '.join(offenders))


def test_permission_fetch_is_prefixed():
    """The nav's permission lookup must respect the /CRM prefix.

    It once used a helper that did not exist (CRM.url), so the request went
    to /api/access/me instead of /CRM/api/access/me, 404'd, and left the
    permission set empty — which silently removed the Deals, Reports and
    Access groups from everyone's navigation.
    """
    html = open(os.path.join(_TEMPLATES, 'app.html')).read()
    assert "crmUrl('/api/access/me')" in html, \
        'the /api/access/me fetch must go through crmUrl()'
    assert 'CRM.url(' not in html, \
        'CRM.url() does not exist — the helper is crmUrl()'


def test_missing_permissions_do_not_empty_the_menu():
    """An unknown permission set must show everything, not nothing.

    The server enforces access independently, so failing open costs a
    refused click; failing closed costs the user their whole menu.
    """
    html = open(os.path.join(_TEMPLATES, 'app.html')).read()
    assert 'var MY_PERMS = null;' in html
    assert 'if (MY_PERMS === null) return true;' in html


def test_header_right_side_cannot_be_squeezed():
    """The nav grew to eight groups and overlapped the user controls.

    flex-shrink:0 on .top-r is what stops the two sides colliding; without
    it the nav pushes straight through Sign out and the user chip.
    """
    html = open(os.path.join(_TEMPLATES, 'app.html')).read()
    top_r = [l for l in html.split('\n') if l.startswith('.top-r{')]
    assert top_r, '.top-r rule not found'
    assert 'flex-shrink:0' in top_r[0], \
        '.top-r must not shrink, or the nav overlaps the header controls'


def test_no_duplicate_intelligence_control():
    """The Intelligence chip duplicated the Intelligence menu group."""
    html = open(os.path.join(_TEMPLATES, 'app.html')).read()
    assert 'id="newsBell"' not in html, \
        'the duplicate Intelligence chip is back, crowding the header'
