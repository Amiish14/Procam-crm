"""No odd green button on the One RFQ page.

UI sweep 19 Nov item 7: "+ Add rate line" was the only green button in
the product's toolbars.

The rule this settles on, rather than "no green anywhere": green marks
an action that CONCLUDES something -- Approve, Mark Won, Save PO. Adding
a row to a table concludes nothing, and neither does submitting the
form that adds it, so both take the primary style every other action on
the page uses.

`.btn.success` is left in the stylesheet on purpose. Four buttons on
other screens use it for exactly the concluding actions above; this is
about the two that did not.
"""
import os
import sys

os.environ.setdefault('SECRET_KEY', 'test-only-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Test-123456789')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, 'templates', 'rfq', 'detail.html')


def test_the_page_has_no_green_button_left():
    body = open(PAGE).read()
    assert 'btn success' not in body


def test_add_rate_line_uses_the_primary_style():
    body = open(PAGE).read()
    assert ('<button class="btn primary" onclick="openAddLine()">'
            '+ Add rate line</button>') in body


def test_the_dialogs_submit_matches_every_other_dialog():
    """The same fault, one tap away: leaving it green would have the
    next sweep report this page again."""
    body = open(PAGE).read()
    assert 'class="btn primary" onclick="submitRate()"' in body


def test_the_toolbar_is_now_one_style_throughout():
    body = open(PAGE).read()
    toolbar = body.split('<div class="toolbar">')[1].split('</div>')[0]
    assert 'btn success' not in toolbar
    assert toolbar.count('btn primary') == 2      # Advance, Add rate line
    assert 'class="btn"' in toolbar               # ← All RFQs stays plain


def test_the_success_style_is_still_there_for_the_screens_that_mean_it():
    """Approve, Mark Won and Save PO conclude something. Deleting the
    class would have restyled four buttons nobody asked about."""
    css = open(os.path.join(ROOT, 'static', 'css', 'crm.css')).read()
    assert '.btn.success{' in css
