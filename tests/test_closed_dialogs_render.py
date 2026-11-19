"""A closed dialog is not on the page, at any width.

UI re-check 19 Nov item 3: on a phone the empty "New RFQ" and "New
quote" forms were sitting at the bottom of those pages before anybody
pressed the button.

The browser hides a closed dialog with `dialog:not([open]){display:
none}` in its OWN stylesheet, and ANY author rule beats a UA rule. The
phone sheet in `ui.css` listed a bare `dialog` among the things it
makes `display:flex`, so every closed dialog on the page came back --
full width, 100dvh tall, below the content.

Two changes, because either alone would be a rule somebody has to
remember. The phone sheet now says `dialog[open]`: geometry can apply
to a closed dialog harmlessly, `display` cannot. And `crm.css` states
`dialog:not([open]){display:none}` in its own voice, so the next rule
that sets `display` on a dialog does not reopen this.
"""
import os
import sys

os.environ.setdefault('SECRET_KEY', 'test-only-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Test-123456789')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import glob
import pytest

import render_probe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRM_CSS = os.path.join(ROOT, 'static', 'css', 'crm.css')
UI_CSS = os.path.join(ROOT, 'static', 'css', 'ui.css')

needs_chrome = pytest.mark.skipif(not render_probe.available(),
                                  reason='no Chrome on this machine')

#: Every page in the repo with a `<dialog>` on it. Nothing is stubbed
#: per page -- the harness renders a missing record as blank, which is
#: enough for a page's chrome and its dialogs to lay out.
PAGES = (
    'company/detail.html',
    'company/list.html',
    'competitor/master.html',
    'handover/queue.html',
    'quote/detail.html',
    'quote/list.html',
    'rfq/detail.html',
    'rfq/list.html',
)

PROBE = """
function probe(){
  var out = [];
  [].slice.call(D.querySelectorAll('dialog')).forEach(function(d){
    var r = d.getBoundingClientRect();
    out.push({id: d.id || '(no id)', open: d.hasAttribute('open'),
              display: W.getComputedStyle(d).display,
              h: Math.round(r.height), w: Math.round(r.width)});
  });
  return {dialogs: out,
          pageOverflow: D.documentElement.scrollWidth - W.innerWidth};
}
"""


def _render(page, width):
    return render_probe.render(page, PROBE, width=width)


@needs_chrome
@pytest.mark.parametrize('page', PAGES)
@pytest.mark.parametrize('width', [360, 441, 767, 768, 1255])
def test_no_closed_dialog_is_drawn(page, width):
    r = _render(page, width)
    assert r['dialogs'], (page, 'the page has no dialog to check')
    for d in r['dialogs']:
        assert d['open'] is False, (page, d['id'], 'opened by itself')
        assert d['display'] == 'none', (page, width, d['id'], d['display'])
        assert d['h'] == 0 and d['w'] == 0, (page, width, d)


@needs_chrome
@pytest.mark.parametrize('page', PAGES)
def test_the_phone_sheet_is_where_this_went_wrong(page):
    """767 is inside the phone sheet and 768 is not, and the fault only
    ever showed below 768. Worth saying which side of the line it was
    on, so a fix at the wrong breakpoint is caught."""
    for width in (767, 768):
        for d in _render(page, width)['dialogs']:
            assert d['display'] == 'none', (page, width, d['id'])


@needs_chrome
def test_an_open_dialog_is_still_a_full_height_sheet_on_a_phone():
    """The point of the rule being fixed: it has to keep working."""
    opened = ("<script>document.getElementById('createModal')"
              ".showModal();</script>")
    r = render_probe.render('rfq/list.html', PROBE, width=441,
                            extra_body=opened)
    d = [x for x in r['dialogs'] if x['id'] == 'createModal'][0]
    assert d['open'] is True
    assert d['display'] == 'flex'
    assert d['w'] >= 420, d['w']
    assert d['h'] >= 500, d['h']


@needs_chrome
def test_an_open_dialog_is_a_centred_card_on_a_desktop():
    opened = ("<script>document.getElementById('createModal')"
              ".showModal();</script>")
    r = render_probe.render('rfq/list.html', PROBE, width=1255,
                            extra_body=opened)
    d = [x for x in r['dialogs'] if x['id'] == 'createModal'][0]
    assert d['open'] is True
    assert d['display'] != 'none'
    assert d['w'] <= 700, (d['w'], 'the desktop dialog is max-width 660')


# ── holds without Chrome ─────────────────────────────────────────────
def test_the_phone_sheet_only_claims_open_dialogs():
    css = open(UI_CSS).read()
    assert '.mod, .mod-sm, .mod-lg, .modal, dialog[open] {' in css
    # The base rule at the top of the Dialogs section still names a
    # bare `dialog`, and should: it sets background, border and shadow,
    # none of which a hidden element draws. It is `display` that must
    # never reach a closed dialog, so that is what is checked.
    sheet = css[css.index('@media (max-width: 767px) {'):]
    block = sheet[:sheet.index('\n}\n')]
    for rule in block.split('}'):
        if 'display:' in rule.replace(' ', '') and 'dialog' in rule:
            assert 'dialog[open]' in rule, rule.strip()[:120]


def test_the_closed_state_is_stated_in_our_own_stylesheet():
    css = open(CRM_CSS).read()
    assert 'dialog:not([open]){display:none}' in css
    assert 'dialog:not([open]){display:none !important}' not in css, (
        'a script that shows a dialog by inline style should still win')


def test_every_dialog_in_the_repo_is_covered():
    """A page with a dialog that nobody measures is how this comes
    back."""
    found = {os.path.relpath(p, os.path.join(ROOT, 'templates'))
             for p in glob.glob(os.path.join(ROOT, 'templates', '**',
                                             '*.html'), recursive=True)
             if '<dialog' in open(p).read()}
    assert found == set(PAGES), found.symmetric_difference(set(PAGES))
