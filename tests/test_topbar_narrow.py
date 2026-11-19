"""The shared top bar fits the screen, and keeps its links.

UI sweep 19 Nov item 3: on seven pages the bar was 505px wide in a
441px screen, the page scrolled sideways, and "Back to CRM" was cut
off its end.

`nav{display:none}` below 820px was already there -- it stopped the nav
overflowing and took every section with it, so on a phone the only way
to Quotes or RFQs was to know the URL. The four toolbar buttons it left
behind are what overflowed.

Both now live in one <details>. On a desktop the details and its panel
are `display:contents`, so the bar is laid out exactly as before.
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


def _phone_block():
    body = open(CSS).read()
    i = body.index('@media (max-width:820px)')
    depth, j = 0, body.index('{', i)
    for k in range(j, len(body)):
        if body[k] == '{':
            depth += 1
        elif body[k] == '}':
            depth -= 1
            if depth == 0:
                return body[i:k]
    raise AssertionError('the 820px block is not closed')


def test_the_nav_and_the_toolbar_are_both_inside_the_details():
    body = open(BAR).read()
    panel = body.split('<div class="crm-more-panel">')[1].split('</details>')[0]
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


def test_the_desktop_bar_is_not_changed():
    body = open(CSS).read()
    assert '.crm-more,.crm-more-panel{display:contents}' in body
    assert '.crm-more > summary{display:none}' in body


def test_a_browser_without_display_contents_wraps_instead_of_breaking():
    body = open(CSS).read()
    assert '@supports not (display:contents)' in body


def test_the_phone_gets_one_menu_button_at_a_finger_size():
    said = _phone_block()
    assert '.crm-more > summary{' in said
    assert 'min-height:44px' in said


def test_the_nav_is_no_longer_simply_hidden():
    said = _phone_block()
    assert '.crm-top nav{display:none}' not in said.replace(' ', ''), (
        'hiding the nav takes every section away on a phone')


def test_the_panel_cannot_be_wider_than_the_screen():
    said = _phone_block()
    assert 'width:min(290px,calc(100vw - 24px))' in said


def test_the_panel_scrolls_rather_than_running_off_the_bottom():
    said = _phone_block()
    assert 'max-height:calc(100vh - 80px)' in said
    assert 'overflow-y:auto' in said


def test_the_panel_overrides_the_navs_own_overflow_hidden():
    """`.crm-top nav` is `overflow:hidden`, which would clip the menu."""
    said = _phone_block()
    assert '.crm-more-panel nav{display:block;overflow:visible}' in said


def test_the_marker_triangle_is_not_drawn():
    said = _phone_block()
    assert 'list-style:none' in said
    assert '::-webkit-details-marker{display:none}' in said


def test_every_item_in_the_menu_is_a_finger_tall():
    said = _phone_block()
    block = said.split('.crm-more-panel nav a,')[1][:220]
    assert 'min-height:44px' in block


def test_the_summary_says_what_it_opens():
    body = open(BAR).read()
    assert 'aria-label="Sections and tools"' in body
