"""
The panel renders the conversation fields — confidence, how it
answered, clarification options, follow-up chips, citations — escaped,
and sends the page's record and the conversation id back.

Most checks read the template. The rendering check runs the panel's own
renderAnswer() under node when node is installed, feeding it markup in
every field, because "escaped" is only true if a hostile string comes
out inert.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src():
    with open(os.path.join(_ROOT, 'templates', '_copilot_panel.html')) as fh:
        return fh.read()


def _function(src, name):
    """The source of one top-level function in the panel script."""
    start = src.index(f'function {name}(')
    depth, i = 0, src.index('{', start)
    while True:
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1


def test_the_panel_sends_the_record_and_the_conversation():
    src = _src()
    ask = _function(src, 'askIt')
    assert 'payload.conversation_id = conversationId' in ask
    assert 'payload.context = {type: context.type, id: context.id}' in ask
    assert 'conversationId = j.conversation_id' in _function(src, 'remember')


def test_the_panel_finds_the_record_the_way_the_pages_link_to_it():
    detect = _function(_src(), 'detectContext')
    assert "data-copilot-type" in detect and "data-copilot-id" in detect
    # /companies/<id> is plural; the old pattern never matched it
    assert 'companies' in detect
    for param in ("'lead'", "'opp'", "'account'"):
        assert param in detect
    assert 'window.ProcamAI.setContext' in _src()


def test_a_new_conversation_forgets_the_old_one():
    src = _src()
    block = src[src.index("getElementById('paiNew').onclick"):]
    block = block[:block.index('};') + 2]
    assert 'conversationId = null' in block and 'history = []' in block


def test_every_new_field_goes_through_esc():
    render = _function(_src(), 'renderAnswer')
    for field in ('j.how_answered', 'clar.question', 'o.label', 'c.label',
                  'c.source', 'esc(f)'):
        assert field in render, field
    # nothing from the answer is concatenated into markup unescaped
    for raw in re.findall(r"\+\s*(j\.[a-z_]+|clar\.[a-z]+|o\.[a-z]+|"
                          r"c\.[a-z]+|f)\s*\+", render):
        pytest.fail(f'unescaped {raw} in renderAnswer')


def test_options_and_chips_ask_through_the_normal_path():
    wire = _function(_src(), 'wireChips')
    assert '[data-clar]' in wire and 'askIt(o.question)' in wire
    assert '[data-next]' in wire and 'askIt(f)' in wire
    assert '[data-cite]' in wire and 'openRecord(cites[' in wire


@pytest.mark.skipif(shutil.which('node') is None, reason='node not installed')
def test_the_panel_runs_end_to_end_against_a_stub_page():
    """Open on /companies/12, ask, render, continue the conversation,
    start a new one, and let a page set the record — no runtime error,
    the record and the conversation id go out, the answer comes back
    escaped."""
    harness = os.path.join(_ROOT, 'tests', 'fixtures',
                           'copilot_panel_smoke.js')
    template = os.path.join(_ROOT, 'templates', '_copilot_panel.html')
    out = subprocess.run(['node', harness, template], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr or out.stdout
    assert 'PANEL_SMOKE_OK' in out.stdout


@pytest.mark.skipif(shutil.which('node') is None, reason='node not installed')
def test_hostile_strings_come_out_inert(tmp_path):
    src = _src()
    js = '\n'.join([
        _function(src, 'esc'),
        "var CONF_LABEL = {high: 'High confidence', medium: "
        "'Medium confidence', low: 'Low confidence'};",
        _function(src, 'renderAnswer'),
        'var bad = "<img src=x onerror=alert(1)>";',
        'var j = {prose: bad, confidence: "high", how_answered: bad,',
        '  clarification: {question: bad, options: [{label: bad, '
        '  question: bad}]},',
        '  follow_ups: [bad], citations: [{type: "lead", id: 1, label: bad,'
        '  source: bad, date: bad}], sources: [bad], notes: [bad],',
        '  columns: ["A"], rows: [{A: bad}], figures: {}, log_id: 1};',
        'process.stdout.write(JSON.stringify(renderAnswer(j)));',
    ])
    path = tmp_path / 'render.js'
    path.write_text(js)
    out = subprocess.run(['node', str(path)], capture_output=True, text=True,
                         timeout=30)
    assert out.returncode == 0, out.stderr
    html = json.loads(out.stdout)
    assert '<img' not in html
    assert html.count('&lt;img src=x onerror=alert(1)&gt;') >= 8
    assert 'High confidence' in html
    assert 'data-clar="0"' in html and 'data-next="0"' in html
    assert 'data-cite="0"' in html
