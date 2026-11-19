"""The mail ingest log is a card list on a phone.

UI sweep 19 Nov item 5: the table was 877px wide in a 441px screen and
"Create lead" -- the only action on the page -- was cut off its end.

Two things were missing and the second is the one that would have been
easy to get wrong: the `.tw` wrapper the card view looks for, and a
real `<thead>`. A browser inserts a `tbody` on its own and never a
`thead`, and `lists.js` reads its labels from `thead tr` -- so the
wrapper alone would have given unlabelled cards with the header row
sitting among them as a card of its own.
"""
import os
import sys

os.environ.setdefault('SECRET_KEY', 'test-only-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Test-123456789')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, 'templates', 'mailops', 'ingest_log.html')
SHELL = os.path.join(ROOT, 'templates', 'mailops', '_shell.html')
LISTS_JS = os.path.join(ROOT, 'static', 'js', 'lists.js')


def test_the_table_is_inside_the_wrapper():
    body = open(LOG).read()
    i = body.index('<div class="tw">')
    j = body.index('<table class="mo">', i)
    assert j > i
    assert body.index('</table>', j) < body.index('</div>', body.index('</table>', j) - 1) + 20


def test_the_header_row_is_in_a_real_thead():
    body = open(LOG).read()
    head = body.split('<thead>')[1].split('</thead>')[0]
    for th in ('Received', 'From', 'Subject', 'Outcome', 'Lead', 'Why'):
        assert f'<th>{th}</th>' in head, th


def test_the_rows_are_in_a_real_tbody():
    body = open(LOG).read()
    inside = body.split('<tbody>')[1].split('</tbody>')[0]
    assert '{% for r in rows %}' in inside


def test_the_stamper_reads_its_labels_from_thead():
    """Why the thead is not optional."""
    body = open(LISTS_JS).read()
    assert "table.querySelector('thead tr')" in body


def test_the_shell_loads_the_card_stylesheet_after_its_own_styles():
    """`table.mo th,td` and `.tw tbody td` have the same specificity, so
    source order decides -- and the card view has to win on a phone."""
    body = open(SHELL).read()
    assert body.index('</style>') < body.index('static/css/lists.css')


def test_the_shell_loads_the_stamper():
    body = open(SHELL).read()
    assert 'static/js/lists.js' in body
    assert 'lists.js?v={{ asset_v }}' in body


def test_the_action_column_has_an_empty_heading():
    """`lists.js` reads an unlabelled cell holding a control as the
    card's footer, which is where "Create lead" belongs."""
    body = open(LOG).read()
    head = body.split('<thead>')[1].split('</thead>')[0]
    assert '<th></th>' in head
    assert 'Create lead' in body
