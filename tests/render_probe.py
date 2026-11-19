"""Render a template in a real browser and measure what it laid out.

UI re-check 19 Nov item 1 is why this exists. The top bar's markup was
right, the stylesheet was right, and the bar still showed nothing but
the logo: a closed `<details>` hides its content inside the UA's own
shadow tree, and no author stylesheet reaches past that. A test that
reads the template or the CSS as text cannot see it -- only a browser
that has actually done the layout can.

So this drives the Chrome that is already on the machine: headless,
with a probe script appended to the page that writes what it measured
into an element, and `--dump-dom` to read it back. Nothing is
installed, and `available()` is false wherever Chrome is not, so the
suite still runs on the server.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

import jinja2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHROME = next(
    (p for p in (
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        '/Applications/Chromium.app/Contents/MacOS/Chromium',
        shutil.which('google-chrome') or '',
        shutil.which('chromium') or '',
        shutil.which('chromium-browser') or '',
    ) if p and os.path.exists(p)),
    None)


def available():
    return CHROME is not None


#: The handful of globals the CRM's templates reach for. Everything is
#: permitted, so the page under test carries every link it ever would.
CONTEXT = dict(
    url_prefix='', asset_v='t', request=type('R', (), {'path': '/rfqs'})(),
    can=lambda *_: True, feature=lambda *_: True, feature_for=lambda *_: True,
    csrf_token=lambda: 'x',
)

PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="{root}/static/css/tokens.css">
<link rel="stylesheet" href="{root}/static/css/crm.css">
<link rel="stylesheet" href="{root}/static/css/ui.css">
{extra_css}
</head><body>
{body}
</body></html>"""

#: The page under test is measured inside an IFRAME of exactly the
#: width asked for, not in the browser window. Chrome on a Mac will
#: not make a window narrower than about 466px, so `--window-size=390`
#: and `--window-size=441` both laid out at 466 and a phone width could
#: not be tested at all. An iframe is its own viewport: media queries,
#: `100vw` and `innerWidth` all answer to its width, at any size.
HOST = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>html,body{{margin:0;padding:0}}
iframe{{border:0;display:block;width:{width}px;height:{height}px}}</style>
</head><body>
<iframe id="under" src="page.html"></iframe>
<pre id="renderProbe"></pre>
<script>
function run(){{
  var out;
  try {{
    var f = document.getElementById('under');
    window.W = f.contentWindow;
    window.D = f.contentDocument;
    {probe}
    out = probe();
  }} catch (e) {{ out = {{probeError: String(e) + ' @ ' + e.stack}}; }}
  document.getElementById('renderProbe').textContent =
    JSON.stringify(out, null, 1);
}}
/* The host's own load event: the iframe's document does not exist
   while this script is being parsed, so anything that reaches for it
   here throws and the probe never runs at all. */
window.addEventListener('load', run);
</script></body></html>"""


def render(template, probe, width=1255, height=900, extra_css='',
           extra_body='', scale=1):
    """Render `template` in an iframe `width` wide, run `probe` against
    it in Chrome, and return what it measured.

    `probe` is JavaScript declaring `function probe()` and returning a
    JSON-able object. Inside it, `D` is the document under test and `W`
    its window -- NOT the host page's, which is why nothing in a probe
    says plain `document`.
    """
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(os.path.join(ROOT, 'templates')),
        extensions=['jinja2.ext.do'])
    env.globals.update(CONTEXT)
    body = env.get_template(template).render(**CONTEXT) + extra_body
    page = PAGE.format(root='file://' + ROOT, body=body, extra_css=extra_css)
    host = HOST.format(width=width, height=height, probe=probe)

    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, 'page.html'), 'w') as fh:
            fh.write(page)
        host_path = os.path.join(tmp, 'host.html')
        with open(host_path, 'w') as fh:
            fh.write(host)
        out = subprocess.run(
            [CHROME, '--headless=new', '--disable-gpu', '--no-sandbox',
             '--hide-scrollbars', '--allow-file-access-from-files',
             '--force-device-scale-factor=%s' % scale,
             '--window-size=%d,%d' % (max(width + 40, 900), height + 120),
             '--virtual-time-budget=4000', '--dump-dom',
             '--user-data-dir=' + os.path.join(tmp, 'profile'),
             'file://' + host_path],
            capture_output=True, text=True, timeout=120)
    dom = out.stdout
    m = re.search(r'<pre id="renderProbe">(.*?)</pre>', dom, re.S)
    if not m or not m.group(1).strip():
        raise AssertionError('the page never ran its probe:\n'
                             + (out.stderr or dom)[-2000:])
    text = (m.group(1).replace('&lt;', '<').replace('&gt;', '>')
            .replace('&amp;', '&').replace('&quot;', '"'))
    got = json.loads(text)
    if isinstance(got, dict) and 'probeError' in got:
        raise AssertionError('the probe threw: ' + got['probeError'])
    return got
