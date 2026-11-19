"""CRM-UI-01 Stage 4: forms and modals are phone-first (CSS only)."""
import os
import re

CSS = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'static', 'css', 'ui.css')).read()


def test_dialogs_become_full_screen_sheets_on_a_phone():
    block = CSS[CSS.index('Stage 4: forms and modals'):]
    assert '@media (max-width: 767px)' in block
    for rule in ('min-height: 100dvh', 'position: sticky; bottom: 0',
                 'grid-template-columns: 1fr !important',
                 'font-size: 16px !important', 'min-height: 44px'):
        assert rule in block, rule


#: Comments stripped before anything is scanned. The rule being
#: checked is "nothing in Stage 4 hides an element", and a comment
#: that EXPLAINS a `display:none` -- the note on `dialog[open]` says
#: the browser's own `dialog:not([open]){display:none}` loses to any
#: author rule -- is prose, not a rule. A test nobody can edit a
#: comment past is a test that gets edited instead.
def _rules(text):
    return re.sub(r'/\*.*?\*/', '', text, flags=re.S)


def test_stage_four_is_layout_only():
    block = _rules(CSS[CSS.index('Stage 4: forms and modals'):])
    assert not re.search(r'display:\s*none', block)
