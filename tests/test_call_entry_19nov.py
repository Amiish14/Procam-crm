"""CRM-UI-01: every call / activity entry is one component."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    return open(os.path.join(ROOT, *parts)).read()


def test_the_component_and_its_server_twin_draw_the_same_rows():
    js = _read('static', 'js', 'call_entry.js')
    macro = _read('templates', '_call_entry.html')
    for label in ('Contact', 'Outcome', 'Notes', 'Next'):
        assert f"'{label}'" in js
        assert f'>{label}<' in macro
    assert 'pc-call-when' in js and 'pc-call-when' in macro


def test_every_view_uses_it():
    app_html = _read('templates', 'app.html')
    assert 'static/js/call_entry.js' in app_html
    # account timeline, lead call notes, contact card
    assert app_html.count('callEntry({') >= 3
    assert 'call_entry(' in _read('templates', 'company', 'detail.html')
    assert 'call_entry(' in _read('templates', 'pic', 'detail.html')


def test_it_is_display_only():
    js = _read('static', 'js', 'call_entry.js')
    assert 'fetch(' not in js and 'POST' not in js
