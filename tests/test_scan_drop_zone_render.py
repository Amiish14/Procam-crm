"""The two drop zones are full-width dashed boxes, one above the other.

UI re-check 19 Nov item 2. The sweep before it stopped the native file
control being painted inside the zone, by clipping the input. That was
right and it was not enough: a `<label>` is INLINE, and `.drop` never
said otherwise. It got away with it only because the file control was
an in-flow box wide and tall enough to give the line something to be.

Clip that input and the label has no in-flow content left. An inline
box takes no height from its vertical padding and no width from
`width`, so both zones collapsed to a sliver at the left edge, their
padding overflowed and they overlapped one another, and the second
line was cut to "cepts...".

Measured in a browser rather than read out of the stylesheet, because
the stylesheet was not wrong -- the missing declaration was never
there to read.
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
CSS = os.path.join(ROOT, 'static', 'css', 'crm.css')

needs_chrome = pytest.mark.skipif(not render_probe.available(),
                                  reason='no Chrome on this machine')

WIDTHS = [360, 390, 441, 768, 1255]

PROBE = """
function probe(){
  var a = D.getElementById('drop'), b = D.getElementById('drop-back');
  var box = function(el){
    var r = el.getBoundingClientRect();
    return {w:Math.round(r.width), h:Math.round(r.height),
            l:Math.round(r.left), t:Math.round(r.top),
            bottom:Math.round(r.bottom)};
  };
  var front = box(a), back = box(b);
  /* Text wider than the box it sits in: this is "Accepts..." cut to
     "cepts...", measured rather than looked at. */
  var cut = [].slice.call(D.querySelectorAll('.drop div')).filter(
    function(d){ return d.scrollWidth > d.clientWidth + 1; }).map(
    function(d){ return d.textContent.trim().slice(0, 24); });
  var inner = a.parentElement.getBoundingClientRect().width;
  return {
    front: front, back: back, inner: Math.round(inner),
    overlap: front.bottom > back.t,
    frontDisplay: W.getComputedStyle(a).display,
    backDisplay: W.getComputedStyle(b).display,
    /* The input is still in the document and still focusable -- it is
       what makes the whole zone clickable. */
    inputs: D.querySelectorAll('.drop input[type=file]').length,
    inputDisplay: W.getComputedStyle(D.getElementById('file')).display,
    inputWidth: Math.round(
      D.getElementById('file').getBoundingClientRect().width),
    cut: cut,
    pageOverflow: D.documentElement.scrollWidth - W.innerWidth
  };
}
"""


@pytest.fixture(scope='module', params=WIDTHS)
def shot(request):
    return request.param, render_probe.render(
        'business_card/scan.html', PROBE, width=request.param)


@needs_chrome
def test_each_zone_is_a_block_box(shot):
    width, r = shot
    assert r['frontDisplay'] == 'block', width
    assert r['backDisplay'] == 'block', width


@needs_chrome
def test_each_zone_fills_the_width_it_is_given(shot):
    """A sliver at the left edge is the symptom; this is the measure."""
    width, r = shot
    for which in ('front', 'back'):
        assert r[which]['w'] >= r['inner'] - 4, (width, which, r[which]['w'],
                                                 'of', r['inner'])


@needs_chrome
def test_the_zones_do_not_overlap(shot):
    width, r = shot
    assert r['overlap'] is False, (width, r['front'], r['back'])
    assert r['back']['t'] >= r['front']['bottom'], width


@needs_chrome
def test_the_back_of_the_card_is_its_own_box_below_the_front(shot):
    width, r = shot
    assert r['back']['t'] > r['front']['t'], width
    assert r['back']['l'] == r['front']['l'], width


@needs_chrome
def test_each_zone_is_tall_enough_to_hold_its_text(shot):
    width, r = shot
    assert r['front']['h'] >= 90, (width, r['front']['h'])
    assert r['back']['h'] >= 60, (width, r['back']['h'])


@needs_chrome
def test_no_line_of_text_is_cut_off(shot):
    """"Accepts JPG, PNG, HEIC..." read as "cepts..." at 441px."""
    width, r = shot
    assert r['cut'] == [], (width, r['cut'])


@needs_chrome
def test_the_page_does_not_scroll_sideways(shot):
    width, r = shot
    assert r['pageOverflow'] <= 0, (width, r['pageOverflow'])


@needs_chrome
def test_the_native_control_is_still_there_and_still_not_drawn(shot):
    """Clipped, never `display:none`: a clipped input keeps keyboard
    focus, and it is the input inside the label that makes the whole
    zone clickable. `display:none` would take the scanner away from
    anybody working down the page with a keyboard."""
    width, r = shot
    assert r['inputs'] == 2, width
    assert r['inputDisplay'] != 'none', width
    assert r['inputWidth'] <= 2, (width, r['inputWidth'])


# ── holds without Chrome ─────────────────────────────────────────────
def test_the_label_is_given_a_block_box_in_the_stylesheet():
    css = open(CSS).read()
    block = css[css.index('.drop{'):css.index('.drop:hover')]
    assert 'display:block' in block, (
        'a <label> is inline, and with the input clipped there is no '
        'in-flow box left to give the zone a size')
