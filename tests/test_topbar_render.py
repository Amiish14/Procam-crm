"""The top bar, measured in a browser that has actually laid it out.

UI re-check 19 Nov item 1, a live regression I shipped in b31c268.
Everything a text test can read was right -- the markup had one copy
of every link, the stylesheet said `display:contents` at desktop
width -- and the bar still showed nothing but the logo.

A CLOSED `<details>` hides its own content inside the UA's shadow tree
(the `::details-content` slot in current Chrome, an anonymous slot
before it). An author stylesheet cannot reach past that, so
`display:contents` on the panel changed nothing, the summary was
`display:none` at desktop width as designed, and every section was
shut inside a menu with no way to open it.

Nothing short of a real layout catches that, which is what
`render_probe` is for: the Chrome already on the machine, headless,
with a probe script that measures and `--dump-dom` to read it back.
It is skipped where Chrome is not installed, so the server's suite is
unaffected -- hence the markup assertions at the end, which hold
everywhere and would at least catch the `<details>` coming back.
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
BAR = os.path.join(ROOT, 'templates', '_crm_topbar.html')
CSS = os.path.join(ROOT, 'static', 'css', 'crm.css')

needs_chrome = pytest.mark.skipif(not render_probe.available(),
                                  reason='no Chrome on this machine')

#: Where the bar becomes one Menu button. Its own breakpoint, wider
#: than the phone block: nineteen links wrap to three rows on a 900px
#: tablet and a 147px sticky bar is worse than a button.
MENU_BELOW = 1024

PROBE = """
function probe(){
  var bar = document.querySelector('.crm-top');
  var vis = function(el){
    if (!el) return false;
    var r = el.getBoundingClientRect();
    return el.offsetParent !== null && r.width > 0 && r.height > 0;
  };
  var links = [].slice.call(bar.querySelectorAll('nav a, .toolbar a'));
  var name = function(a){ return a.textContent.trim(); };
  return {
    barHeight: Math.round(bar.getBoundingClientRect().height),
    barOverflow: bar.scrollWidth - bar.clientWidth,
    pageOverflow: document.documentElement.scrollWidth - window.innerWidth,
    total: links.length,
    shown: links.filter(vis).map(name),
    hidden: links.filter(function(a){ return !vis(a); }).map(name),
    menuButton: vis(document.querySelector('.crm-more-btn')),
    panelDisplay: getComputedStyle(
      document.querySelector('.crm-more-panel')).display,
    tap: Math.round((document.querySelector('.crm-more-btn')
      .getBoundingClientRect().height) || 0)
  };
}
"""

#: Opened by hand, which is what a phone does to it.
OPEN = "<script>document.getElementById('crmMoreToggle').checked=true;</script>"


@pytest.fixture(scope='module')
def wide():
    return render_probe.render('_crm_topbar.html', PROBE, width=1440)


@pytest.fixture(scope='module')
def laptop():
    """1255px: the width the re-check was done at."""
    return render_probe.render('_crm_topbar.html', PROBE, width=1255)


@pytest.fixture(scope='module')
def tablet():
    return render_probe.render('_crm_topbar.html', PROBE, width=900)


@pytest.fixture(scope='module')
def phone():
    return render_probe.render('_crm_topbar.html', PROBE, width=441)


@pytest.fixture(scope='module')
def phone_open():
    return render_probe.render('_crm_topbar.html', PROBE, width=441,
                               extra_body=OPEN)


# ── the regression itself ────────────────────────────────────────────
@needs_chrome
@pytest.mark.parametrize('width', [1024, 1255, 1366, 1440, 1920])
def test_a_desktop_shows_every_link(width):
    r = render_probe.render('_crm_topbar.html', PROBE, width=width)
    assert r['hidden'] == [], (width, 'links not laid out')
    assert r['total'] == len(r['shown']) >= 15, (width, r['total'])


@needs_chrome
@pytest.mark.parametrize('width', [1024, 1255, 1366, 1440, 1920])
def test_a_desktop_has_no_menu_button(width):
    r = render_probe.render('_crm_topbar.html', PROBE, width=width)
    assert r['menuButton'] is False, width
    assert r['panelDisplay'] == 'contents', (width, r['panelDisplay'])


# ── and the overflow that was behind it ──────────────────────────────
@needs_chrome
@pytest.mark.parametrize('width', [1024, 1255, 1366, 1440, 1920])
def test_nothing_runs_off_the_screen(width):
    """Opening the menu by hand made the page 1383px on a 1255px
    screen. The links wrap now instead of overflowing, and nothing is
    cut off -- `overflow:hidden` on the nav was the old answer and it
    hid the last two sections rather than showing them."""
    r = render_probe.render('_crm_topbar.html', PROBE, width=width)
    assert r['barOverflow'] <= 0, (width, r['barOverflow'])
    assert r['pageOverflow'] <= 0, (width, r['pageOverflow'])


@needs_chrome
def test_the_bar_wraps_rather_than_growing_without_limit(laptop, wide):
    """Two rows at the widths people use. Three would be a sticky bar
    taking a sixth of the window, which is what the tighter spacing
    between 821 and 1399px is for."""
    assert 56 <= laptop['barHeight'] <= 100, laptop['barHeight']
    assert 56 <= wide['barHeight'] <= 100, wide['barHeight']


# ── the phone, which must keep working ───────────────────────────────
@needs_chrome
def test_a_phone_gets_one_button_and_a_closed_menu(phone):
    assert phone['menuButton'] is True
    assert phone['panelDisplay'] == 'none'
    assert phone['shown'] == []
    assert phone['barHeight'] <= 72, phone['barHeight']
    assert phone['tap'] >= 44, ('the button is under the tap minimum',
                                phone['tap'])


@needs_chrome
def test_a_tablet_gets_the_menu_too(tablet):
    """900px: nineteen links wrapped to three rows there."""
    assert tablet['menuButton'] is True
    assert tablet['panelDisplay'] == 'none'


@needs_chrome
def test_the_menu_opens_and_holds_every_link(phone_open):
    assert phone_open['panelDisplay'] == 'block'
    assert phone_open['hidden'] == [], 'a link in the open menu is not drawn'
    assert len(phone_open['shown']) == phone_open['total'] >= 15


@needs_chrome
def test_the_open_menu_does_not_widen_the_page(phone_open):
    """It is positioned over the page, not in it: `width:min(290px,
    calc(100vw - 24px))`."""
    assert phone_open['pageOverflow'] <= 0, phone_open['pageOverflow']


# ── holds without Chrome, so the server's suite still guards it ──────
def test_the_bar_is_not_a_details_element():
    """A closed `<details>` hides its content in a shadow tree no
    author stylesheet reaches. This is the regression, in one line."""
    body = open(BAR).read()
    assert '<details' not in body
    assert '<summary' not in body
    assert 'class="crm-more-toggle"' in body
    assert 'for="crmMoreToggle"' in body


def test_the_toggle_is_a_checkbox_so_it_needs_no_script():
    body = open(BAR).read()
    assert 'type="checkbox"' in body
    assert '<label class="crm-more-btn"' in body


def test_there_is_still_one_copy_of_every_link():
    """A duplicated phone menu would be a second list to keep in step
    with the permissions, and the first thing to drift."""
    body = open(BAR).read()
    for href in ('/quotes', '/rfqs/chain', '/global-crm', '/help'):
        assert body.count('"{{ url_prefix }}%s"' % href) == 1, href


def test_the_panel_is_shown_by_the_checked_toggle():
    css = open(CSS).read()
    assert '.crm-more-toggle:checked ~ .crm-more-panel{display:block}' in css
    assert '.crm-more-toggle,.crm-more-btn{display:none}' in css


def test_the_nav_wraps_instead_of_hiding_what_does_not_fit():
    css = open(CSS).read()
    bar = css[css.index('.crm-top nav{'):css.index('.crm-top nav a{')]
    assert 'flex-wrap:wrap' in bar
    assert 'overflow:hidden' not in bar, (
        'this is what cut the last two sections off')


def test_the_menu_has_its_own_breakpoint_above_the_phone_block():
    css = open(CSS).read()
    assert '@media (max-width:%dpx)' % (MENU_BELOW - 1) in css
    at = css.index('@media (max-width:%dpx)' % (MENU_BELOW - 1))
    block = css[at:css.index('/* ── Page header', at)]
    assert '.crm-more{display:block' in block
