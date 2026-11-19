"""The TMS handover queue is a card list on a phone.

Not in the re-check: the probe harness measured it while item 4 was
being fixed, and at 441px the page scrolled 304px sideways -- ten
columns, with TMS Project, Acknowledged and the Acknowledge button
itself past the right-hand edge. Asked for and agreed after the
re-check.

Same machinery as the Leads, RFQ and quote lists: the stylesheet, the
`.tw` wrapper and the script, and nothing in the ten-column render
function except one class.

That class is the point of `test_the_row_number_is_not_on_the_card`.
`lists.css` hides a row-number cell on a card -- the account is the
card's heading and "# 412" above it is noise -- but it keys on
`td.tbl-num[data-label="#"]`, so the cell has to carry the class or
the rule has nothing to match.

The closed dialogs on this page need nothing: 105c56f fixed that for
every dialog in the CRM. They are asserted here anyway, because this
page has two and they are the kind that would be noticed last.
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
PAGE = os.path.join(ROOT, 'templates', 'handover', 'queue.html')

needs_chrome = pytest.mark.skipif(not render_probe.available(),
                                  reason='no Chrome on this machine')

COLUMNS = ['#', 'Account', 'Vertical', 'Won value', 'Opp / Quote',
           'Customer PO', 'Status', 'TMS Project', 'Acknowledged', '']

#: Three handovers, by hand: the page fetches its own and there is no
#: server in a test, so an empty tbody would report that the table
#: fits whatever is done to it.
ROWS = """<script>
document.getElementById('tbody').innerHTML = [401,402,403].map(function(i){
  return '<tr>'
   + '<td class="tbl-num">' + i + '</td>'
   + '<td><b>Hindustan Unilever Limited</b></td><td>FMCG</td>'
   + '<td class="num">1,24,56,000</td><td>Opp #88 / Q#214</td>'
   + '<td>PO/2026/00' + i + '</td>'
   + '<td><span class="pill sent">Awaiting TMS</span></td>'
   + '<td>PRJ-2026-0' + i + '</td><td>2026-11-18</td>'
   + '<td><button class="btn tiny">Acknowledge</button></td></tr>';
}).join('');
</script>"""

PROBE = """
function probe(){
  var tb = D.getElementById('tbody');
  var tds = [].slice.call(tb.querySelectorAll('td'));
  var table = tb.closest('table');
  var has = function(c){ return tds.filter(function(t){
    return t.classList.contains(c); }).length; };
  return {
    pageOverflow: D.documentElement.scrollWidth - W.innerWidth,
    tableWidth: Math.round(table.getBoundingClientRect().width),
    wrapped: !!table.closest('.tw'),
    rows: tb.querySelectorAll('tr').length,
    rowDisplay: W.getComputedStyle(tb.querySelector('tr')).display,
    headDisplay: W.getComputedStyle(D.querySelector('thead')).display,
    labels: tds.slice(0, 10).map(function(t){
      return t.getAttribute('data-label'); }),
    numberDisplay: W.getComputedStyle(tds[0]).display,
    titles: has('is-title'),
    actions: has('is-actions'),
    cut: tds.filter(function(t){
      return t.scrollWidth > t.clientWidth + 1; }).length,
    offRight: tds.filter(function(t){
      return t.getBoundingClientRect().right > W.innerWidth + 1; }).length,
    dialogs: [].slice.call(D.querySelectorAll('dialog')).map(function(d){
      return {id: d.id, open: d.hasAttribute('open'),
              display: W.getComputedStyle(d).display}; })
  };
}
"""


def _shot(width):
    return render_probe.render('handover/queue.html', PROBE, width=width,
                               extra_body=ROWS)


@pytest.fixture(scope='module')
def phone():
    return _shot(441)


@pytest.fixture(scope='module')
def desktop():
    return _shot(1255)


@needs_chrome
@pytest.mark.parametrize('width', [360, 390, 441, 600, 767])
def test_nothing_runs_off_a_phone_screen(width):
    """304px of it, at 441."""
    r = _shot(width)
    assert r['pageOverflow'] <= 0, (width, r['pageOverflow'])
    assert r['offRight'] == 0, (width, 'cells past the edge')
    assert r['cut'] == 0, (width, 'cells with content cut off')


@needs_chrome
def test_each_row_is_a_card(phone):
    assert phone['wrapped'] is True
    assert phone['rowDisplay'] == 'block'
    assert phone['headDisplay'] == 'none'
    assert phone['rows'] == 3


@needs_chrome
def test_every_cell_carries_its_columns_name(phone):
    assert phone['labels'] == COLUMNS, phone['labels']


@needs_chrome
def test_the_account_is_the_cards_heading(phone):
    """Not the row number: `lists.js` skips `#` when it picks the
    heading, so the first real column becomes it."""
    assert phone['titles'] == 3, phone['titles']


@needs_chrome
def test_the_row_number_is_not_on_the_card(phone):
    """It is a desktop convenience. The rule keys on
    `td.tbl-num[data-label="#"]`, so the cell carries the class -- the
    one line of the render function this needed."""
    assert phone['numberDisplay'] == 'none', phone['numberDisplay']


@needs_chrome
def test_the_acknowledge_button_is_the_cards_footer(phone):
    """A control column has no heading, so `lists.js` puts it along the
    bottom rather than down the side."""
    assert phone['actions'] == 3, phone['actions']


@needs_chrome
def test_neither_dialog_is_drawn_while_closed(phone):
    for d in phone['dialogs']:
        assert d['open'] is False, d['id']
        assert d['display'] == 'none', (d['id'], d['display'])
    assert len(phone['dialogs']) == 2


@needs_chrome
def test_the_desktop_is_still_a_table_with_its_row_numbers(desktop):
    assert desktop['rowDisplay'] == 'table-row'
    assert desktop['headDisplay'] == 'table-header-group'
    assert desktop['numberDisplay'] != 'none'
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


def test_the_card_view_has_the_last_word_at_a_phone_width():
    """After the page's own <style> block, so whatever is added to it
    later cannot take the layout back."""
    body = open(PAGE).read()
    assert body.index('</style>') < body.index('css/lists.css')


def test_the_script_runs_after_the_rows_are_rendered():
    body = open(PAGE).read()
    assert body.index('<tbody id="tbody"') < body.index('js/lists.js')


def test_the_row_number_cell_carries_the_class_the_rule_needs():
    body = open(PAGE).read()
    assert '<td class="tbl-num">${r.id}</td>' in body
    assert '<th class="tbl-num">#</th>' in body
