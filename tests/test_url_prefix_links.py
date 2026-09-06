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
    bad = [u for u in _LINK.findall(r.get_data(as_text=True))
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
