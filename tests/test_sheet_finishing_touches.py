"""Three finishing touches after the 19 Nov sweep passed.

1. The open menu reached the floating chat button, so "Back to CRM" --
   the last item in the list, and the one most likely to be wanted --
   sat behind the pill. The button steps aside, which is what it
   already does while a dialog is open.

2. Every `<dialog>` on the module pages put Cancel at the BOTTOM of
   its form. On a phone that form is a full-height sheet, so getting
   out of one meant scrolling to the end of it. They now carry a
   pinned close, like the account modal.

   Written by `static/js/sheets.js` rather than into ten templates.
   The button is the same button every time and the eleventh dialog
   somebody adds should have it without being told; ten copies in ten
   templates is ten things to keep in step and the eleventh goes
   without.

3. `/scan` 404'd -- the page has always been at
   `/business-cards/scan`, and `/scan` is the name people type.
"""
import os
import sys

os.environ.setdefault('SECRET_KEY', 'test-only-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Test-123456789')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest

import render_probe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRM_CSS = os.path.join(ROOT, 'static', 'css', 'crm.css')
UI_CSS = os.path.join(ROOT, 'static', 'css', 'ui.css')
BAR = os.path.join(ROOT, 'templates', '_crm_topbar.html')
SHEETS = os.path.join(ROOT, 'static', 'js', 'sheets.js')

needs_chrome = pytest.mark.skipif(not render_probe.available(),
                                  reason='no Chrome on this machine')

#: Every page with a dialog, and the id of one to open on it.
DIALOGS = [
    ('rfq/list.html', 'createModal'),
    ('rfq/detail.html', 'addLineModal'),
    ('quote/list.html', 'm'),
    ('quote/detail.html', 'lineModal'),
    ('competitor/master.html', 'createModal'),
    ('company/list.html', 'newCo'),
    ('company/detail.html', 'tagDlg'),
    ('handover/queue.html', 'poModal'),
]

OPEN_MENU = ("<script>document.getElementById('crmMoreToggle')"
             ".checked = true;</script>")

MENU_PROBE = """
function probe(){
  var fab = D.getElementById('paiFab');
  var panel = D.querySelector('.crm-more-panel');
  var items = [].slice.call(panel.querySelectorAll('a'));
  var last = items[items.length - 1];
  var f = fab.getBoundingClientRect(), l = last.getBoundingClientRect();
  var hits = items.filter(function(a){
    var r = a.getBoundingClientRect();
    return !(r.bottom < f.top || r.top > f.bottom ||
             r.right < f.left || r.left > f.right);
  }).map(function(a){ return a.textContent.trim(); });
  return {fabDisplay: W.getComputedStyle(fab).display,
          lastItem: last.textContent.trim(),
          behindTheButton: hits,
          items: items.length};
}
"""

#: The dialog is opened and closed INSIDE the probe, which runs on the
#: host's load event. A script appended to the page body runs before
#: `DOMContentLoaded`, which is when `sheets.js` writes the button --
#: so a click from there hits nothing, and says the button is broken
#: when it is the test that is early.
SHEET_PROBE = """
function probe(){
  var d = D.getElementById('DLG');
  var fabNow = function(){ return W.getComputedStyle(
    D.getElementById('paiFab')).display; };
  var out = {closes: D.querySelectorAll('.sheet-x').length,
             dialogs: D.querySelectorAll('dialog').length,
             fabAtRest: fabNow()};
  d.showModal();
  var x = d.querySelector('.sheet-x');
  out.hasClose = !!x;
  if (!x) return out;
  var r = x.getBoundingClientRect();
  out.label = x.getAttribute('aria-label');
  out.type = x.getAttribute('type');
  out.isFirstChild = x.parentElement.firstElementChild === x;
  out.inTheScrollingBox = x.parentElement !== d;
  out.position = W.getComputedStyle(x).position;
  out.size = [Math.round(r.width), Math.round(r.height)];
  out.onScreen = r.top >= 0 && r.left >= 0
                 && r.right <= W.innerWidth + 1;
  out.openBefore = d.hasAttribute('open');
  out.fabWhileOpen = fabNow();
  x.click();
  out.openAfter = d.hasAttribute('open');
  return out;
}
"""


# ── 1. the menu and the floating button ──────────────────────────────
@needs_chrome
@pytest.mark.parametrize('width', [360, 441, 767, 1024, 1279])
def test_nothing_in_the_open_menu_sits_behind_the_chat_button(width):
    r = render_probe.render('_crm_topbar.html', MENU_PROBE, width=width,
                            extra_body=OPEN_MENU)
    assert r['fabDisplay'] == 'none', (width, r['fabDisplay'])
    assert r['behindTheButton'] == [], (width, r['behindTheButton'])


@needs_chrome
@pytest.mark.parametrize('width', [441, 1279])
def test_the_last_item_is_the_one_that_was_covered(width):
    r = render_probe.render('_crm_topbar.html', MENU_PROBE, width=width,
                            extra_body=OPEN_MENU)
    assert r['lastItem'] == 'Back to CRM', r['lastItem']
    assert r['items'] >= 15, r['items']


@needs_chrome
@pytest.mark.parametrize('width', [441, 1279])
def test_the_chat_button_comes_back_when_the_menu_is_shut(width):
    """It is the whole point of the button; hiding it for good would be
    a worse fault than the one being fixed."""
    r = render_probe.render('_crm_topbar.html', MENU_PROBE, width=width)
    assert r['fabDisplay'] != 'none', width


# ── 2. the pinned close on every sheet ───────────────────────────────
@needs_chrome
@pytest.mark.parametrize('page,dlg', DIALOGS)
@pytest.mark.parametrize('width', [441, 1255])
def test_every_dialog_carries_a_pinned_close(page, dlg, width):
    r = render_probe.render(page, SHEET_PROBE.replace('DLG', dlg),
                            width=width)
    assert r['hasClose'] is True, (page, width)
    assert r['closes'] == r['dialogs'], (page, 'one per dialog, no more')
    assert r['label'] == 'Close', page
    assert r['type'] == 'button', (page, 'a submit would post the form')
    assert r['position'] == 'sticky', page
    assert r['isFirstChild'] is True, (page, 'it has to be at the top')
    assert r['inTheScrollingBox'] is True, (
        page, 'a sticky button outside the box that scrolls does not stick')
    assert r['onScreen'] is True, (page, width)


@needs_chrome
@pytest.mark.parametrize('page,dlg', DIALOGS)
def test_the_close_actually_closes(page, dlg):
    """`method="dialog"` forms close themselves on submit; a button
    added by script has to say so."""
    r = render_probe.render(page, SHEET_PROBE.replace('DLG', dlg), width=441)
    assert r['openBefore'] is True, page
    assert r['openAfter'] is False, (page, 'the dialog stayed open')


@needs_chrome
@pytest.mark.parametrize('page,dlg', DIALOGS[:4])
def test_the_close_is_a_finger_wide_on_a_phone(page, dlg):
    r = render_probe.render(page, SHEET_PROBE.replace('DLG', dlg), width=441)
    assert r['size'] == [44, 44], (page, r['size'])


@needs_chrome
@pytest.mark.parametrize('page,dlg', DIALOGS[:4])
def test_the_chat_button_steps_aside_while_a_sheet_is_open(page, dlg):
    """It sits over the sheet's own sticky footer, so a tap meant for
    Create RFQ could land on Procam AI. `shell.css` says this for the
    single-page app and these pages do not load `shell.css`, so until
    now `sheets.js` was setting a class nothing here read."""
    r = render_probe.render(page, SHEET_PROBE.replace('DLG', dlg), width=441)
    assert r['fabAtRest'] != 'none', page
    assert r['fabWhileOpen'] == 'none', page


# ── 3. /scan ─────────────────────────────────────────────────────────
@pytest.fixture(scope='module')
def flask_app():
    import app as A
    A.app.config['TESTING'] = True
    return A.app


@pytest.fixture(scope='module')
def client(flask_app):
    """Signed in as an admin, because the alias is gated exactly like
    the page it stands for (`module.business_cards`). A signed-out
    client would only ever see the refusal, which is what
    `test_the_alias_is_gated_like_the_page_it_stands_for` checks."""
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess.update(emp_code='DIR12010', name='Tech team', role='admin',
                    vertical='All', sv=0)
    return c


@pytest.fixture(scope='module')
def signed_out(flask_app):
    return flask_app.test_client()


def test_scan_redirects_to_the_page(client):
    r = client.get('/scan')
    assert r.status_code == 302, r.status_code
    assert r.headers['Location'].endswith('/business-cards/scan')


def test_scan_carries_the_prefix_the_crm_is_served_under(client):
    """`url_for`, not a hard-coded path: behind nginx the CRM is at
    /CRM and a bare /business-cards/scan would 404 just as /scan did."""
    r = client.get('/scan', environ_overrides={'SCRIPT_NAME': '/CRM'})
    assert r.status_code == 302
    assert r.headers['Location'] == '/CRM/business-cards/scan'


def test_the_real_page_is_still_where_it_was(client):
    """The alias is an addition, not a move: every link already built
    with url_for points at the long path."""
    r = client.get('/business-cards/scan')
    assert r.status_code in (200, 302), r.status_code
    if r.status_code == 302:
        assert 'business-cards/scan' not in r.headers['Location'], (
            'it should be the sign-in, not a redirect loop')


def test_the_alias_is_gated_like_the_page_it_stands_for(signed_out):
    """An alias more open than its destination is a second answer to
    "who may reach the scanner". Signed out, both answer the same way
    -- and neither of them answers with the page.
    """
    bare = signed_out.get('/business-cards/scan')
    alias = signed_out.get('/scan')
    assert bare.status_code != 200, 'the page itself is not public'
    assert alias.status_code == bare.status_code, (
        alias.status_code, bare.status_code)
    if alias.status_code in (301, 302):
        assert not alias.headers['Location'].endswith(
            '/business-cards/scan'), 'it let the way through unchallenged'


def test_the_alias_is_listed_nowhere_as_public():
    """`tests/test_every_route_has_a_gate.py` refuses an ungated route
    unless it is listed public with a reason. This one is gated, so it
    must not also be sitting on that list."""
    import re
    src = open(os.path.join(ROOT, 'scripts',
                            'generate_reference_docs.py')).read()
    block = re.search(r'PUBLIC_ROUTES\s*=\s*[\[\({](.*?)[\]\)}]',
                      src, re.S)
    if block:
        assert "'/scan'" not in block.group(1)


def test_it_is_not_a_permanent_redirect(client):
    """301 is cached by the browser for good, and if the page ever
    moves again every bookmark keeps pointing at a path nobody can
    change."""
    assert client.get('/scan').status_code != 301


# ── holds without Chrome ─────────────────────────────────────────────
def test_the_button_is_hidden_by_the_open_menu_in_the_stylesheet():
    css = open(CRM_CSS).read()
    assert '.crm-top:has(.crm-more-toggle:checked) ~ .pai-fab{display:none}' \
        in css


def test_there_is_a_fallback_where_has_is_unsupported():
    """The button cannot be reached from the bar without `:has()`, so
    the list keeps clear of it instead."""
    css = open(CRM_CSS).read()
    assert '@supports not selector(:has(*))' in css
    at = css.index('@supports not selector(:has(*))')
    assert 'padding-bottom' in css[at:at + 160]


def test_the_module_pages_read_modal_open_too():
    css = open(CRM_CSS).read()
    assert 'body.modal-open .pai-fab{display:none}' in css


def test_the_close_is_written_once_and_not_into_ten_templates():
    import glob
    assert os.path.exists(SHEETS)
    assert 'sheets.js' in open(BAR).read(), (
        'every page with a dialog includes the bar')
    for tpl in glob.glob(os.path.join(ROOT, 'templates', '**', '*.html'),
                         recursive=True):
        body = open(tpl).read()
        if '<dialog' not in body:
            continue
        assert 'class="sheet-x"' not in body, (
            os.path.basename(tpl), 'a hand-written copy of the shared button')


def test_the_close_has_a_phone_size_and_a_desktop_size():
    assert 'dialog .sheet-x{' in open(CRM_CSS).read()
    assert 'dialog .sheet-x {' in open(UI_CSS).read()
