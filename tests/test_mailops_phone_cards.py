"""Every mail-ops table is a card list on a phone.

UI sweep 19 Nov item 5 fixed the ingest log and left fifteen sibling
pages with the identical shape: a `table.mo` with no `.tw` wrapper and
no `<thead>`, so on a 441px screen they were an 800-odd-pixel table
with the last column off the end.

The machinery is already there -- `lists.css` and `lists.js` load in
`mailops/_shell.html`, so every one of these pages needed exactly two
things: the wrapper, and a header row that really is a `<thead>`.

Two parts of that are easy to get wrong and both are pinned below:

* A browser inserts a `<tbody>` by itself and NEVER a `<thead>`, so a
  header row left as a bare first `<tr>` ends up inside the tbody.
  `lists.js` reads its labels from `thead tr` and finds none, and the
  cards come out unlabelled with the header row sitting among them as
  a card of its own.
* `lists.css` has to be the last word on a phone. `table.mo th, td`
  in the shell's own `<style>` and `.tw tbody td` have the SAME
  specificity, so source order decides -- hence the link after the
  block rather than before it.
"""
import glob
import os
import re
import sys

os.environ.setdefault('SECRET_KEY', 'test-only-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Test-123456789')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jinja2
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAILOPS = os.path.join(ROOT, 'templates', 'mailops')
SHELL = os.path.join(MAILOPS, '_shell.html')

#: A form laid out as a table, not a list of records: the left cell is
#: a label and the right one its input. It gets the wrapper, so each
#: field stacks under its own label, and no header -- there is nothing
#: to name the columns.
NO_HEADER = {'crm_rules.html'}

TABLE = re.compile(r'<table\b[^>]*>')


def _pages():
    return sorted(p for p in glob.glob(os.path.join(MAILOPS, '*.html'))
                  if os.path.basename(p) != '_shell.html')


def _tables(body):
    """Each `<table …>…</table>` block, outermost only (none nest here)."""
    out = []
    for m in TABLE.finditer(body):
        end = body.index('</table>', m.end())
        out.append((m.start(), body[m.start():end + len('</table>')]))
    return out


def _ids():
    return [os.path.basename(p) for p in _pages() if '<table' in open(p).read()]


@pytest.fixture(scope='module')
def pages():
    return {os.path.basename(p): open(p).read() for p in _pages()}


# ── every table ──────────────────────────────────────────────────────
@pytest.mark.parametrize('name', _ids())
def test_every_table_is_wrapped(pages, name):
    body = pages[name]
    for at, block in _tables(body):
        before = body[:at]
        assert before.rstrip().endswith('<div class="tw">'), (
            name, 'table not inside a .tw wrapper', block[:70])


@pytest.mark.parametrize('name', _ids())
def test_every_wrapper_is_closed(pages, name):
    body = pages[name]
    assert body.count('<div class="tw">') == len(_tables(body)), name
    for at, block in _tables(body):
        after = body[at + len(block):].lstrip()
        assert after.startswith('</div>'), (name, 'wrapper left open')


@pytest.mark.parametrize('name', _ids())
def test_the_header_row_is_a_real_thead(pages, name):
    """Not a bare first `<tr>`: `lists.js` reads `thead tr`."""
    for _, block in _tables(body := pages[name]):
        if name in NO_HEADER:
            assert '<th' not in block, (name, 'has headers but no thead')
            continue
        assert '<thead>' in block and '</thead>' in block, name
        assert '<tbody>' in block and '</tbody>' in block, name
        head = block[block.index('<thead>'):block.index('</thead>')]
        assert '<th' in head, (name, 'thead with no th in it')
        # the header row is the FIRST row, and the only one above tbody
        assert block.index('<thead>') < block.index('<tbody>'), name
        assert head.count('<tr') == 1, (name, 'more than one header row')


@pytest.mark.parametrize('name', _ids())
def test_no_row_is_left_outside_thead_or_tbody(pages, name):
    """A `<tr>` between `</thead>` and `<tbody>`, or after `</tbody>`,
    is the row a browser would re-home somewhere surprising."""
    for _, block in _tables(pages[name]):
        if name in NO_HEADER:
            continue
        spine = re.sub(r'<thead>.*?</thead>', '', block, flags=re.S)
        spine = re.sub(r'<tbody>.*?</tbody>', '', spine, flags=re.S)
        assert '<tr' not in spine, (name, 'a row sits outside both')


# ── the two tables that never had a header row ───────────────────────
def test_the_flag_tables_got_the_names_switches_already_uses(pages):
    """`health.html` and `rules.html` list the same four things
    `switches.html` does and had no header row at all, so the card view
    had nothing to label with. The names are taken from the page that
    already had them rather than invented twice."""
    want = ('<tr><th>Switch</th><th>What it does</th><th>Set in</th>'
            '<th>On</th></tr>')
    for name in ('health.html', 'rules.html'):
        assert want in pages[name], name
    assert '<th>Switch</th>' in pages['switches.html']


# ── the shell, which is what makes all of the above work ─────────────
def test_the_shell_loads_both_halves():
    body = open(SHELL).read()
    assert 'css/lists.css' in body
    assert 'js/lists.js' in body


def test_lists_css_comes_after_the_shells_own_table_styles():
    """Same specificity, so whichever is written second wins, and the
    one that has to win on a phone is the card view."""
    body = open(SHELL).read()
    assert body.index('table.mo th') < body.index('css/lists.css')


def test_the_script_is_after_the_body_block():
    body = open(SHELL).read()
    assert body.index('{% block body %}') < body.index('js/lists.js')


# ── the pages still compile ──────────────────────────────────────────
def test_every_page_still_parses_as_jinja():
    """Moving markup around a `{% for %}` is exactly how a block ends up
    unbalanced, and a template only fails when somebody opens it."""
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(os.path.join(ROOT, 'templates')),
        extensions=['jinja2.ext.do'])
    env.globals.update(url_prefix='', asset_v='1', can=lambda *_: True,
                       feature=lambda *_: True, feature_for=lambda *_: True,
                       csrf_token=lambda: 'x')
    for p in _pages() + [SHELL]:
        env.get_template('mailops/' + os.path.basename(p))
