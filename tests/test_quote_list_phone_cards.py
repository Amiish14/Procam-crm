"""The Quotes list is a card list on a phone, like the RFQ list.

UI re-check 19 Nov item 4: at 441px it was still a plain table with
Currency, Prepared by and Date off the right-hand edge, and the page
scrolled sideways.

Nothing here is new work. `lists.css` turns a table inside a `.tw`
wrapper into cards under 768px and `lists.js` stamps every cell with
its own column's heading, so the page needed the stylesheet, the
wrapper and the script -- and not one line of its eight-column render
function.

Measured with rows in it. The page fetches its own quotes and there is
no server in a test, so an empty tbody would say the table fits
whatever is done to it: the probe puts four quotes in by hand first.
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
PAGE = os.path.join(ROOT, 'templates', 'quote', 'list.html')

needs_chrome = pytest.mark.skipif(not render_probe.available(),
                                  reason='no Chrome on this machine')

COLUMNS = ['Quote #', 'Subject', 'Status', 'Rev', 'Total', 'Currency',
           'Prepared by', 'Date']

#: Four quotes, by hand. Long enough in the subject and the total to
#: be what actually overflowed.
ROWS = """<script>
document.getElementById('tbody').innerHTML = [1,2,3,4].map(function(i){
  return '<tr onclick="void 0">'
    + '<td><b>PQ-2026-000' + i + '</b></td>'
    + '<td>Project cargo, Nhava Sheva to Jebel Ali, 2 x 40HC</td>'
    + '<td><span class="pill sent">Sent</span></td>'
    + '<td>' + i + '</td><td class="num">12,45,600.00</td>'
    + '<td>INR</td><td>Pre-sales desk</td><td>2026-11-19</td></tr>';
}).join('');
</script>"""

PROBE = """
function probe(){
  var tb = D.getElementById('tbody');
  var tds = [].slice.call(tb.querySelectorAll('td'));
  var table = tb.closest('table');
  var head = D.querySelector('thead');
  return {
    viewport: W.innerWidth,
    pageOverflow: D.documentElement.scrollWidth - W.innerWidth,
    tableWidth: Math.round(table.getBoundingClientRect().width),
    wrapped: !!table.closest('.tw'),
    rows: tb.querySelectorAll('tr').length,
    rowDisplay: W.getComputedStyle(tb.querySelector('tr')).display,
    headDisplay: W.getComputedStyle(head).display,
    labels: tds.slice(0, 8).map(function(td){
      return td.getAttribute('data-label'); }),
    /* A cell whose content is wider than the cell: this is the column
       cut off at the edge, measured rather than looked at. */
    cut: tds.filter(function(td){
      return td.scrollWidth > td.clientWidth + 1; }).length,
    offRight: tds.filter(function(td){
      return td.getBoundingClientRect().right > W.innerWidth + 1; }).length,
    /* The card's heading is the first real column. */
    titles: tds.filter(function(td){
      return td.classList.contains('is-title'); }).length
  };
}
"""


@pytest.fixture(scope='module')
def phone():
    return render_probe.render('quote/list.html', PROBE, width=441,
                               extra_body=ROWS)


@pytest.fixture(scope='module')
def desktop():
    return render_probe.render('quote/list.html', PROBE, width=1255,
                               extra_body=ROWS)


@needs_chrome
@pytest.mark.parametrize('width', [360, 390, 441, 600, 767])
def test_nothing_runs_off_a_phone_screen(width):
    r = render_probe.render('quote/list.html', PROBE, width=width,
                            extra_body=ROWS)
    assert r['pageOverflow'] <= 0, (width, r['pageOverflow'])
    assert r['offRight'] == 0, (width, r['offRight'], 'cells past the edge')
    assert r['cut'] == 0, (width, r['cut'], 'cells with content cut off')


@needs_chrome
def test_each_row_is_a_card(phone):
    assert phone['rowDisplay'] == 'block'
    assert phone['headDisplay'] == 'none', 'the header row is not a card'
    assert phone['rows'] == 4


@needs_chrome
def test_every_cell_carries_its_columns_name(phone):
    """Stamped from the `<th>`, so a column renamed stays right in both
    views and no render function mentions a label."""
    assert phone['labels'] == COLUMNS, phone['labels']


@needs_chrome
def test_each_card_has_exactly_one_heading(phone):
    assert phone['titles'] == 4, phone['titles']


@needs_chrome
def test_the_desktop_is_still_a_table(desktop):
    assert desktop['rowDisplay'] == 'table-row'
    assert desktop['headDisplay'] == 'table-header-group'
    assert desktop['pageOverflow'] <= 0
    assert desktop['tableWidth'] > 1000, desktop['tableWidth']


# ── holds without Chrome ─────────────────────────────────────────────
def test_the_page_loads_both_halves_of_the_card_view():
    body = open(PAGE).read()
    assert 'css/lists.css' in body
    assert 'js/lists.js' in body


def test_the_table_is_wrapped():
    body = open(PAGE).read()
    at = body.index('<table>')
    assert body[:at].rstrip().endswith('<div class="tw">')


def test_the_script_runs_after_the_rows_are_rendered():
    """`lists.js` watches the tbody for replacement, but it has to see
    the tbody first."""
    body = open(PAGE).read()
    assert body.index('<tbody id="tbody"') < body.index('js/lists.js')


def test_the_stylesheet_is_the_last_one_in():
    body = open(PAGE).read()
    assert body.index('css/ui.css') < body.index('css/lists.css')
