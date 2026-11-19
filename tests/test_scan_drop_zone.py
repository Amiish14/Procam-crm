"""The scan page shows one drop zone, not two file controls.

UI sweep 19 Nov item 1: raw "Choose File" inputs were drawn over the
drop zone, the dashed frame looked collapsed, and on the back-of-card
label the control sat on top of the text.

The cause was not the scan page. `crm.css` styles `input,select,
textarea` with a border, 11px of padding and a fixed height -- 34px,
42px on a phone -- and `input[type=file]` is an input, so Chrome's own
button was being painted inside a label whose whole job is to be the
button.
"""
import os
import re
import sys

os.environ.setdefault('SECRET_KEY', 'test-only-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Test-123456789')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRM_CSS = os.path.join(ROOT, 'static', 'css', 'crm.css')
SCAN = os.path.join(ROOT, 'templates', 'business_card', 'scan.html')


def _rule(name):
    body = open(CRM_CSS).read()
    found = re.search(re.escape(name) + r'\s*\{([^}]*)\}', body)
    assert found, f'{name} has gone from crm.css'
    return found.group(1)


def test_the_native_file_control_is_not_drawn():
    said = _rule('.drop input[type="file"]')
    assert 'position:absolute' in said
    assert 'width:1px' in said and 'height:1px' in said
    assert 'clip-path:inset(50%)' in said


def test_it_is_clipped_and_not_display_none():
    """A clipped input still takes keyboard focus. `display:none` would
    take the scanner away from anybody working down the page with a
    keyboard."""
    said = _rule('.drop input[type="file"]')
    assert 'display:none' not in said.replace(' ', '')


def test_the_zone_shows_the_focus_it_is_holding():
    said = _rule('.drop:focus-within')
    assert 'border-color' in said and 'box-shadow' in said


def test_the_zone_still_has_its_dashed_frame_and_padding():
    said = _rule('.drop')
    assert 'dashed' in said
    assert 'padding:var(--s-10) var(--s-5)' in said
    # position:relative, so the clipped input is positioned against the
    # zone rather than against the page.
    assert 'position:relative' in said


def test_both_inputs_are_inside_the_label_that_triggers_them():
    """What makes the whole zone clickable. Moving either input out of
    its label would leave a zone that does nothing."""
    body = open(SCAN).read()
    for input_id in ('id="file"', 'id="file-back"'):
        before = body.split(input_id)[0]
        # the nearest tag opened before it is the label
        assert before.rfind('<label class="drop"') > before.rfind('</label>'), \
            input_id


def test_the_rule_is_scoped_so_other_pages_keep_their_file_boxes():
    """Six other pages show a file input on purpose -- the lead
    attachment, the Excel import, the RFQ attachment. The selector is
    `.drop input[type=file]`, not `input[type=file]`."""
    body = open(CRM_CSS).read()
    assert re.search(r'(^|\n)\s*input\[type="file"\]\s*\{', body) is None, (
        'an unscoped file-input rule would hide the other six')


def test_the_front_box_needs_no_filename_display():
    """It uploads on change, so the progress bar is the feedback. The
    back shows its own line, which is why that one has `back-chosen`."""
    body = open(SCAN).read()
    assert "file.addEventListener('change'" in body
    assert 'uploadFile(file.files[0])' in body
    assert 'back-chosen' in body
