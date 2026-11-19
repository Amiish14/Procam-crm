"""The RFQ list is a card list on a phone, like the Leads list.

UI sweep 19 Nov item 4: at 441px the table ran off the screen and the
subject wrapped one word per line.

Stage 3 already solved this once: `lists.css` turns a table inside a
`.tw` wrapper into cards under 768px, and `lists.js` stamps every cell
with its column's heading so the labels come from the `<th>` rather
than from forty render functions. The RFQ list had neither the wrapper
nor the two files, which is the whole reason it was missed.
"""
import os
import sys

os.environ.setdefault('SECRET_KEY', 'test-only-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Test-123456789')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, 'templates', 'rfq', 'list.html')


def test_the_table_is_inside_the_wrapper_the_card_view_looks_for():
    body = open(PAGE).read()
    i = body.index('<div class="tw">')
    j = body.index('<table>', i)
    assert j > i
    assert body.index('</table>', j) < body.index('</div>', j)


def test_the_page_loads_the_card_stylesheet():
    body = open(PAGE).read()
    assert 'static/css/lists.css' in body


def test_the_page_loads_the_stamper():
    body = open(PAGE).read()
    assert 'static/js/lists.js' in body


def test_the_stamper_is_loaded_after_the_rows_are_rendered():
    """`lists.js` watches each tbody, so the order only has to be
    after the table exists -- but loading it before `loadRfqs` is
    defined would leave the first render unstamped until the 1.2s
    retry."""
    body = open(PAGE).read()
    assert body.index('loadRfqs();') < body.index('static/js/lists.js')


def test_both_files_carry_the_asset_version():
    """Or a phone keeps yesterday's copy and the cards never appear."""
    body = open(PAGE).read()
    for said in ('lists.css?v={{ asset_v }}', 'lists.js?v={{ asset_v }}'):
        assert said in body, said


def test_the_columns_still_have_headings_for_the_labels_to_come_from():
    body = open(PAGE).read()
    head = body.split('<thead>')[1].split('</thead>')[0]
    for th in ('RFQ #', 'Subject', 'Status', 'Received', 'Owner'):
        assert f'<th>{th}</th>' in head, th


def test_opening_a_row_still_goes_through_the_prefix():
    """The card view makes the whole row the tap target, so the
    navigation matters more here than it did."""
    body = open(PAGE).read()
    assert "location=crmUrl('/rfqs/${r.id}')" in body
