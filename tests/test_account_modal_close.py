"""The way out of the account dialog is always reachable.

UI sweep 19 Nov item 6: the floating "Ask Procam AI" button covered
half the Close button on a desktop, and at narrow width Close was not
visible without scrolling.

Two different causes:

  the floating button   `body.modal-open` hides it, and the watcher
                        that sets that class looks for `.mob`. This
                        dialog is `#preSalesModal` and was the one
                        dialog in the app that was not a `.mob`.
  Close below the fold  the Close buttons are written by the forty
                        callers, at the END of their content, inside a
                        card that scrolls. One piece of chrome pinned
                        to the card serves all of them.
"""
import os
import re
import sys

os.environ.setdefault('SECRET_KEY', 'test-only-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Test-123456789')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, 'templates', 'app.html')
SHELL_CSS = os.path.join(ROOT, 'static', 'css', 'shell.css')


def _open_modal():
    body = open(APP).read()
    return body.split('window.openModal = function(html)')[1][:1600]


def test_the_dialog_is_a_mob_so_the_watcher_sees_it():
    said = _open_modal()
    assert "m.className = 'mob'" in said


def test_the_class_is_set_before_the_dialog_is_appended():
    """The watcher for dialogs built on demand tests
    `classList.contains('mob')` on the node as it arrives, so a class
    set afterwards would never be seen."""
    said = _open_modal()
    assert said.index("m.className = 'mob'") < \
        said.index('document.body.appendChild(m)')


def test_modal_open_hides_the_floating_button():
    body = open(SHELL_CSS).read()
    assert 'body.modal-open .pai-fab { display: none !important; }' in body


def test_the_card_carries_a_close_of_its_own():
    said = _open_modal()
    assert 'id="preSalesModalX"' in said
    assert 'onclick="closeModal()"' in said
    assert 'aria-label="Close"' in said


def test_the_content_is_written_into_a_body_not_over_the_chrome():
    """`innerHTML` on the card would wipe the pinned button on every
    open -- it worked once and then never again."""
    said = _open_modal()
    assert "g('preSalesModalBody').innerHTML = html" in said
    assert "g('preSalesModalCard').innerHTML = html" not in said


def test_the_close_is_pinned_while_the_card_scrolls():
    body = open(APP).read()
    rule = re.search(r'#preSalesModalX\s*\{([^}]*)\}', body)
    assert rule, 'the pinned close has gone'
    said = rule.group(1)
    assert 'position: sticky' in said
    assert 'top: 0' in said
    # Out of the content's flow, so no caller has to leave room.
    assert 'float: right' in said


def test_the_card_still_scrolls_for_the_sticky_to_work_against():
    said = _open_modal()
    assert 'overflow-y:auto' in said


def test_it_is_a_finger_wide_on_a_phone():
    body = open(APP).read()
    phone = body.split('@media (max-width: 767px) {')
    assert any('#preSalesModalX { min-width: 44px; min-height: 44px; }' in part
               for part in phone), 'no phone size for the close button'


def test_the_dialog_uses_the_whole_phone_screen():
    body = open(APP).read()
    assert '#preSalesModalCard { width: 100% !important;' in body
    assert 'max-height: 94dvh !important' in body


def test_the_callers_own_close_buttons_are_left_alone():
    """They are still there and still work; this only adds a second way
    out that cannot scroll away."""
    body = open(APP).read()
    # The callers' own buttons, not the pinned one this change added --
    # counting all of them would have counted mine and said nothing.
    theirs = re.findall(r'onclick="closeModal\(\)"\s*>\s*(Close|Cancel)',
                        body)
    assert len(theirs) >= 4, theirs
