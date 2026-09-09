"""Every internal link must carry the deployment prefix.

The portal is served at /CRM behind nginx.  A link written as href="/rfqs"
resolves at the domain root, where nginx knows nothing about it and
answers its own 404 — the app never sees the request.  Templates get
`url_prefix` from a context processor for exactly this.
"""
import os
import re
import sys
import glob
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_PREFIX = '/CRM'
os.environ['URL_PREFIX'] = _PREFIX
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'PrefixTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'prefix.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db, Employee = _main.app, _main.db, _main.Employee

_TEMPLATES = os.path.join(_ROOT, 'templates')

PAGES = ['/app', '/rfqs', '/quotes', '/handovers', '/competitors',
         '/competitors/dashboard', '/reports', '/help', '/notifications',
         '/funnels/account-development', '/business-cards/scan', '/my-work']

_LINK = re.compile(r'(?:href|src|action)="(/[^/"][^"]*)"')


@pytest.fixture(autouse=True)
def _force_prefix():
    """URL_PREFIX is process-global and other test modules clear it.

    The context processor reads it per request (so a deployment can change
    the prefix without a code change), which makes these tests sensitive to
    import order unless it is pinned here.
    """
    previous = os.environ.get('URL_PREFIX')
    os.environ['URL_PREFIX'] = _PREFIX
    yield
    if previous is None:
        os.environ.pop('URL_PREFIX', None)
    else:
        os.environ['URL_PREFIX'] = previous


@pytest.fixture(scope='module')
def client():
    with flask_app.app_context():
        db.create_all()
        e = Employee.query.filter_by(emp_code='PFXADM').first()
        if not e:
            e = Employee(emp_code='PFXADM', name='Prefix Admin')
            db.session.add(e)
        e.role, e.is_active, e.must_change_pw = 'admin', True, False
        e.vertical, e.is_super_admin = 'All', True
        db.session.commit()
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='PFXADM', name='Prefix Admin', role='admin',
                 vertical='All')
    return c


@pytest.mark.parametrize('path', PAGES)
def test_rendered_links_carry_the_prefix(client, path):
    # SCRIPT_NAME is what url_for() reads to build prefixed URLs; nginx
    # supplies it in production via the ProxyFix/force_script_name wrapper.
    # Without it here, correct url_for() links would look broken.
    r = client.get(path, environ_overrides={'SCRIPT_NAME': _PREFIX})
    if r.status_code in (302, 404):
        pytest.skip(f'{path} → {r.status_code}')
    assert r.status_code == 200, f'{path} → {r.status_code}'
    html = r.get_data(as_text=True)
    # Script bodies build URLs from template literals, so a correctly
    # prefixed "/CRM${x.route}" reads as a bare path to this scan. Client
    # calls have their own guard (test_no_client_side_fetch_bypasses...).
    html = re.sub(r'<script\b.*?</script>', '', html, flags=re.S | re.I)
    bad = [u for u in _LINK.findall(html)
           if not u.startswith(_PREFIX + '/')]
    assert not bad, f'{path} has links that bypass {_PREFIX}: {bad[:5]}'


def test_no_template_hardcodes_an_absolute_link():
    """Static guard, so a new template cannot reintroduce this."""
    offenders = []
    pat = re.compile(r'(?:href|src|action)="(/(?!/)[^"]*)"')
    for tpl in glob.glob(os.path.join(_TEMPLATES, '**', '*.html'),
                         recursive=True):
        for n, line in enumerate(open(tpl), 1):
            for url in pat.findall(line):
                if url.startswith('{{'):
                    continue
                offenders.append(
                    f'{os.path.relpath(tpl, _ROOT)}:{n}: {url}')
    assert not offenders, (
        'Absolute links must start with {{ url_prefix }}:\n  '
        + '\n  '.join(offenders))


def test_nav_hrefs_go_through_crm_url():
    """The top navigation builds its anchors in JS, not Jinja."""
    html = open(os.path.join(_TEMPLATES, 'app.html')).read()
    assert "crmUrl(it.href)" in html, \
        'topnav items must resolve their href through crmUrl()'


def test_crm_url_is_safe_for_stored_routes():
    """Routes come out of the database (TaskInstance.action_route,
    Notification.action_url), so the helper has to prefix a bare path,
    leave an already-prefixed one alone, and not touch external links.
    Double-prefixing 404s exactly like not prefixing does.
    """
    with flask_app.test_request_context('/my-work'):
        crm_url = None
        for proc in flask_app.template_context_processors[None]:
            d = proc()
            if 'crm_url' in d:
                crm_url = d['crm_url']
        assert crm_url is not None

        assert crm_url('/app?lead=9') == _PREFIX + '/app?lead=9'
        assert crm_url(_PREFIX + '/app?lead=9') == _PREFIX + '/app?lead=9'
        assert crm_url('https://example.com/x') == 'https://example.com/x'
        assert crm_url('#anchor') == '#anchor'
        assert crm_url(None) == _PREFIX + '/'


def test_my_work_task_links_are_prefixed(client):
    """Clicking a task in My Work 404'd for everyone: action_route is
    stored bare and was rendered straight into the href."""
    from app.models.task_engine import TaskInstance
    with flask_app.app_context():
        if not TaskInstance.query.filter_by(owner_user_id='PFXADM').first():
            db.session.add(TaskInstance(
                task_key='lead.qualify', entity_type='Lead', entity_id=7,
                entity_display='Test Account', owner_user_id='PFXADM',
                status='Pending', priority=2, action_route='/app?lead=7'))
            db.session.commit()

    html = client.get('/my-work',
                      environ_overrides={'SCRIPT_NAME': _PREFIX}
                      ).get_data(as_text=True)
    links = re.findall(r'class="task-row" href="([^"]+)"', html)
    assert links, 'no task rows rendered — the fixture did not take'
    for link in links:
        assert link.startswith(_PREFIX + '/') or link == '#', \
            f'task link bypasses {_PREFIX}: {link}'


def test_no_client_side_fetch_bypasses_the_prefix():
    """A raw fetch('/api/...') 404s under /CRM and the page renders empty.

    This is how the Account, Project and Sales funnels, business-card
    scanning, the RFQ pages, the competitor pages and Help admin all
    loaded successfully and then showed nothing: the HTML was fine, every
    data call missed.

    app.html wraps them in crmUrl(); server-rendered templates prefix the
    literal with url_prefix.
    """
    import re
    pat = re.compile(r"""fetch\(\s*(['"`])(/(?!/)[^'"`]*)""")
    offenders = []
    for tpl in glob.glob(os.path.join(_TEMPLATES, '**', '*.html'),
                         recursive=True):
        for n, line in enumerate(open(tpl), 1):
            for m in pat.finditer(line):
                url = m.group(2)
                if url.startswith('{{'):
                    continue
                offenders.append(
                    f'{os.path.relpath(tpl, _ROOT)}:{n}: fetch("{url}")')
    assert not offenders, (
        'These calls resolve at the domain root, not under the deployment '
        'prefix:\n  ' + '\n  '.join(offenders)
        + '\n\nIn app.html wrap the URL in crmUrl(); elsewhere prefix it '
          'with {{ url_prefix }}.')


@pytest.mark.parametrize('path', [
    '/funnels/account-development',
    '/funnels/project-intelligence',
    '/funnels/sales',
    '/business-cards/scan',
])
def test_the_funnel_and_scan_pages_can_reach_their_data(client, path):
    """They returned 200 while every fetch inside them 404'd."""
    import re
    r = client.get(path, environ_overrides={'SCRIPT_NAME': _PREFIX})
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    calls = re.findall(r"""fetch\(\s*['"`](/[^'"`]*)""", html)
    assert calls, f'{path} makes no data call at all — did it lose its script?'
    bad = [u for u in calls if not u.startswith(_PREFIX + '/')]
    assert not bad, f'{path} calls outside the prefix: {bad}'
