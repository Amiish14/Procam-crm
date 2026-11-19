"""The shared top bar fits the screen, and keeps its links.

UI sweep 19 Nov item 3: on seven pages the bar was 505px wide in a
441px screen, the page scrolled sideways, and "Back to CRM" was cut
off its end.

`nav{display:none}` below 820px was already there -- it stopped the nav
overflowing and took every section with it, so on a phone the only way
to Quotes or RFQs was to know the URL. The four toolbar buttons it left
behind are what overflowed.

Both now live in one collapsible group below 1024px. This file is the
markup-and-stylesheet half, which holds everywhere; the layout itself
is measured in a browser in `test_topbar_render.py`, because the first
attempt at this passed every assertion here and still showed nothing
but the logo on a desktop.
"""
import os
import re
import sys

os.environ.setdefault('SECRET_KEY', 'test-only-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Test-123456789')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAR = os.path.join(ROOT, 'templates', '_crm_topbar.html')
CSS = os.path.join(ROOT, 'static', 'css', 'crm.css')

#: The bar's own breakpoint, wider than the phone block: nineteen
#: links and buttons wrap to three rows on a 900px tablet.
MENU_BELOW = 1280


def _block(at):
    body = open(CSS).read()
    i = body.index(at)
    depth, j = 0, body.index('{', i)
    for k in range(j, len(body)):
        if body[k] == '{':
            depth += 1
        elif body[k] == '}':
            depth -= 1
            if depth == 0:
                return body[i:k]
    raise AssertionError('%s is not closed' % at)


def _menu_block():
    return _block('@media (max-width:%dpx)' % (MENU_BELOW - 1))


def test_the_nav_and_the_toolbar_are_both_inside_the_panel():
    body = open(BAR).read()
    panel = body.split('<div class="crm-more-panel">')[1].split('</header>')[0]
    assert '<nav aria-label="Sections">' in panel
    assert '<div class="spacer toolbar">' in panel
    assert 'Back to CRM' in panel


def test_there_is_exactly_one_copy_of_every_link():
    """A phone menu built by duplicating the markup would be a second
    list to keep in step with the permissions."""
    body = open(BAR).read()
    # The LINK, not the word: the comment above the markup names
    # "Back to CRM" too, and a test that counts prose would be a test
    # nobody can edit a comment past.
    for once in ('>Back to CRM<', '>CRM health<', '>Academy<',
                 '>Quotes<', '>RFQs<', '>My Work<'):
        assert body.count(once) == 1, (once, body.count(once))


def test_the_desktop_bar_draws_the_group_away():
    body = open(CSS).read()
    assert '.crm-more,.crm-more-panel{display:contents}' in body
    assert '.crm-more-toggle,.crm-more-btn{display:none}' in body


def test_a_browser_without_display_contents_wraps_instead_of_breaking():
    body = open(CSS).read()
    assert '@supports not (display:contents)' in body


def test_the_phone_gets_one_menu_button_at_a_finger_size():
    said = _menu_block()
    assert '.crm-more-btn{' in said
    assert 'min-height:44px' in said


def test_the_button_opens_the_panel_with_no_script():
    """The whole reason it is a checkbox: a disclosure element hides its
    own content in a shadow tree an author stylesheet cannot reach, and
    a plain button would need JavaScript -- so a page whose script is
    slow or blocked would have no nav at all."""
    said = _menu_block()
    assert '.crm-more-toggle:checked ~ .crm-more-panel{display:block}' in said
    body = open(BAR).read()
    assert 'type="checkbox"' in body
    assert '<label class="crm-more-btn" for="crmMoreToggle">' in body
    assert '<script' not in body.split('crm-more-panel')[0]


def test_a_closed_panel_is_hidden_rather_than_merely_positioned():
    said = _menu_block()
    panel = said.split('.crm-more-panel{')[1][:60]
    assert panel.lstrip().startswith('display:none'), panel


def test_the_toggle_is_reachable_from_the_keyboard():
    """Clipped, not `display:none`, so it can be tabbed to and opened
    with the space bar; the ring says where focus is."""
    said = _menu_block()
    toggle = said.split('.crm-more-toggle{')[1][:260]
    assert 'display:none' not in toggle
    assert 'clip-path:inset(50%)' in toggle
    assert '.crm-more-toggle:focus-visible ~ .crm-more-btn{' in said


def test_the_open_button_is_marked_as_open():
    said = _menu_block()
    assert '.crm-more-toggle:checked ~ .crm-more-btn{' in said


def test_the_nav_is_no_longer_simply_hidden():
    said = _menu_block()
    assert '.crm-top nav{display:none}' not in said.replace(' ', ''), (
        'hiding the nav takes every section away on a phone')


def test_the_panel_cannot_be_wider_than_the_screen():
    said = _menu_block()
    assert 'width:min(290px,calc(100vw - 24px))' in said


def test_the_panel_scrolls_rather_than_running_off_the_bottom():
    said = _menu_block()
    assert 'max-height:calc(100vh - 80px)' in said
    assert 'overflow-y:auto' in said


def test_the_panel_lays_its_nav_out_down_the_page():
    """The bar's own nav is a wrapping flex row, which inside a 290px
    panel would put three links on a line."""
    said = _menu_block()
    assert '.crm-more-panel nav{display:block;overflow:visible' in said
    assert 'flex-wrap:nowrap' in said


def test_every_item_in_the_menu_is_a_finger_tall():
    said = _menu_block()
    block = said.split('.crm-more-panel nav a,')[1][:220]
    assert 'min-height:44px' in block


def test_the_button_says_what_it_opens():
    body = open(BAR).read()
    label = body.split('<label class="crm-more-btn"')[1].split('</label>')[0]
    assert 'Menu' in label
    assert 'aria-hidden="true"' in label, 'the icon is not read out twice'


def test_the_desktop_bar_wraps_rather_than_clipping():
    body = open(CSS).read()
    bar = body[body.index('.crm-top{'):body.index('.crm-brand{')]
    assert 'flex-wrap:wrap' in bar
    assert 'min-height:56px' in bar
    # `min-height:56px` contains the substring, so look for the
    # declaration itself: a fixed height cannot hold two rows.
    assert not re.search(r'(?<!min-)height:\s*56px', bar), bar


def test_the_tighter_middle_widths_hide_nothing():
    """Between the menu's cut-off and a full desktop the links are set
    tighter, which is what keeps the bar to two rows at 1255px. Padding
    only -- no `display:none`, nothing cut."""
    said = _block('@media (min-width:1280px) and (max-width:1535px)')
    assert 'display:none' not in said
    assert 'overflow:hidden' not in said
    assert '.crm-top nav a{padding:' in said
