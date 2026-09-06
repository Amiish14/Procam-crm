"""Every module page shares one stylesheet, so the portal looks like one
product rather than ten.

Before this, each page carried its own ~60-line copy of the same CSS with
slightly different tokens — the reason the UI drifted apart page to page.
"""
import os
import re
import sys
import glob
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'DesignTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'design.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db, Employee = _main.app, _main.db, _main.Employee

_TEMPLATES = os.path.join(_ROOT, 'templates')
_CSS = os.path.join(_ROOT, 'static', 'css', 'crm.css')

# The single-page app carries the design system itself; login and the
# forced password change are deliberately standalone; offline must work
# with no network at all.
STANDALONE = {'app.html', 'login.html', 'change_password.html',
              'offline.html', '_crm_topbar.html'}


def _module_templates():
    out = []
    for path in glob.glob(os.path.join(_TEMPLATES, '**', '*.html'),
                          recursive=True):
        if os.path.basename(path) in STANDALONE:
            continue
        if '<body' not in open(path).read():
            continue
        out.append(path)
    return out


def test_the_shared_stylesheet_exists():
    assert os.path.exists(_CSS)
    assert len(open(_CSS).read()) > 5000


@pytest.mark.parametrize('path', _module_templates(),
                         ids=lambda p: os.path.relpath(p, _TEMPLATES))
def test_page_uses_the_shared_stylesheet(path):
    src = open(path).read()
    assert 'static/css/crm.css' in src, \
        'every module page must link the shared stylesheet'


@pytest.mark.parametrize('path', _module_templates(),
                         ids=lambda p: os.path.relpath(p, _TEMPLATES))
def test_page_has_no_duplicate_component_css(path):
    """A local <style> block is how the pages drifted apart.

    Page-specific rules are fine — a timeline, a chart — but redefining a
    shared component is what made every page look slightly different.
    Selectors are matched at a rule boundary, not as substrings: `.tl-body`
    is its own component and must not read as a redefinition of `body`.
    """
    src = open(path).read()
    shared = ('btn', 'card', 'pill', 'hero', 'empty', 'kpi', 'filters')
    for block in re.findall(r'<style>(.*?)</style>', src, re.S):
        # Print overrides are page-specific by nature — a certificate
        # restyling `body` for paper is not a component redefinition.
        block = re.sub(r'@media\s+print\s*\{.*?\n  \}', '', block, flags=re.S)
        for comp in shared:
            pattern = r'(?:^|[},;\s])\.' + comp + r'\s*[{,:]'
            hit = re.search(pattern, block, re.M)
            assert hit is None, (
                f'{os.path.basename(path)} redefines .{comp} locally '
                f'({hit.group(0).strip()!r}) — it belongs in '
                f'static/css/crm.css')
        # a bare `body` selector, not `.tl-body`
        assert re.search(r'(?:^|[},;])\s*body\s*[{,]', block, re.M) is None, \
            f'{os.path.basename(path)} restyles body locally'


def test_stylesheet_is_served():
    with flask_app.app_context():
        db.create_all()
    r = flask_app.test_client().get('/static/css/crm.css')
    assert r.status_code == 200
    assert 'css' in r.headers.get('Content-Type', '')


def test_tokens_match_the_portal():
    """The shared sheet and the single-page app must not drift.

    Both define the same accent and surface colours; if one is edited the
    other has to follow, or the two halves of the portal diverge again.
    """
    css = open(_CSS).read()
    app_html = open(os.path.join(_TEMPLATES, 'app.html')).read()
    for token, value in (('--p-red', '#CC1E2E'),
                         ('--p-bg', '#F6F7F9'),
                         ('--p-surface', '#FFFFFF'),
                         ('--p-border', '#E7E9EE'),
                         ('--p-ink', '#0A0B0F')):
        assert f'{token}:{value}' in css.replace(' ', ''), \
            f'{token} missing from the shared stylesheet'
        assert f'{token}:{value}' in app_html.replace(' ', ''), \
            f'{token} changed in app.html but not in crm.css'
