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
import contextlib
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
#:
#: `url_prefix` is the repo itself. The templates build their asset
#: paths as `{{ url_prefix }}/static/css/...`, and with an empty prefix
#: that is an absolute path -- which on `file://` means the root of the
#: disk, so every stylesheet and script 404s silently and the page is
#: measured unstyled. Three pages looked as though they overflowed
#: because of that, and none of them did.
CONTEXT = dict(
    url_prefix='file://' + ROOT, asset_v='t',
    request=type('R', (), {'path': '/rfqs'})(),
    can=lambda *_: True, feature=lambda *_: True, feature_for=lambda *_: True,
    csrf_token=lambda: 'x',
)

class Blank(jinja2.ChainableUndefined):
    """Every name a page reaches for that this harness has not got.

    A detail page wants `company.name`, `rfq.lines`, twenty more; the
    layout being measured does not depend on any of them. Jinja's own
    undefined prints as nothing but raises the moment a template asks
    it for an attribute or walks it, so a page with one record in it
    cannot be rendered at all. This one chains, iterates empty and is
    false, so the page renders its chrome, its tables and its dialogs
    -- which is what gets measured.

    It is deliberately not the application's behaviour: a real missing
    value SHOULD be loud. This is a layout rig.
    """

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def __call__(self, *a, **k):
        """A page may call a helper the app registers rather than read
        a value -- `restriction_level(name)` on the company page. The
        call answers blank like everything else here."""
        return self._new()

    def _new(self):
        return Blank(hint=self._undefined_hint, name=self._undefined_name)


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


#: Chrome is one machine-wide resource, and `-n auto` runs several of
#: these suites at once. Three probes racing for it was the whole of
#: "5 failed" on 19 Nov: the pages were right, the browsers were
#: starting on a loaded machine and one of them never got to a first
#: frame inside its time budget. So the renders queue, across
#: processes, on a lock file -- which is what makes a parallel run of
#: the render suites agree with a serial one.
_LOCK = os.path.join(tempfile.gettempdir(), 'procam-render-probe.lock')


@contextlib.contextmanager
def _only_one_browser():
    try:
        import fcntl
    except ImportError:                       # not POSIX: run unlocked
        yield
        return
    with open(_LOCK, 'a+') as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _chrome(host_path, *, width, height, scale, profile):
    """One headless render, with nobody else rendering at the time."""
    with _only_one_browser():
        return subprocess.run(
            [CHROME, '--headless=new', '--disable-gpu', '--no-sandbox',
             '--hide-scrollbars', '--allow-file-access-from-files',
             '--force-device-scale-factor=%s' % scale,
             '--window-size=%d,%d' % (max(width + 40, 900), height + 120),
             '--virtual-time-budget=4000', '--dump-dom',
             '--user-data-dir=' + profile,
             'file://' + host_path],
            capture_output=True, text=True, timeout=180)


def render(template, probe, width=1255, height=900, extra_css='',
           extra_body='', scale=1, **context):
    """Render `template` in an iframe `width` wide, run `probe` against
    it in Chrome, and return what it measured.

    Anything in `context` is added to the template's own, for a page
    that needs a row or a record to render at all.

    `probe` is JavaScript declaring `function probe()` and returning a
    JSON-able object. Inside it, `D` is the document under test and `W`
    its window -- NOT the host page's, which is why nothing in a probe
    says plain `document`.
    """
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(os.path.join(ROOT, 'templates')),
        extensions=['jinja2.ext.do'], undefined=Blank)
    env.globals.update(CONTEXT)
    body = env.get_template(template).render(**dict(CONTEXT, **context))
    if '<html' in body[:400].lower():
        # A whole page: it has its own head, its own stylesheets and
        # its own viewport meta. Wrapping it in PAGE would nest two
        # documents and put its <link>s in a <body>.
        page = body.replace('</body>', extra_body + '</body>', 1) \
            if '</body>' in body else body + extra_body
        if extra_css:
            page = page.replace('</head>', extra_css + '</head>', 1)
    else:
        page = PAGE.format(root='file://' + ROOT, body=body + extra_body,
                           extra_css=extra_css)
    host = HOST.format(width=width, height=height, probe=probe)

    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, 'page.html'), 'w') as fh:
            fh.write(page)
        host_path = os.path.join(tmp, 'host.html')
        with open(host_path, 'w') as fh:
            fh.write(host)
        out, dom, m = None, '', None
        # Twice at most. `--virtual-time-budget` is a budget in virtual
        # time, but getting to the first frame is real work on a real
        # machine, and a browser that was still starting when the
        # budget ran out dumps a DOM with no measurement in it. That
        # is not a layout fault and must not read as one.
        for _attempt in (1, 2):
            out = _chrome(host_path, width=width, height=height, scale=scale,
                          profile=os.path.join(tmp, 'profile%d' % _attempt))
            dom = out.stdout
            m = re.search(r'<pre id="renderProbe">(.*?)</pre>', dom, re.S)
            if m and m.group(1).strip():
                break
    if not m or not m.group(1).strip():
        raise AssertionError('the page never ran its probe, twice:\n'
                             + (out.stderr or dom)[-2000:])
    text = (m.group(1).replace('&lt;', '<').replace('&gt;', '>')
            .replace('&amp;', '&').replace('&quot;', '"'))
    got = json.loads(text)
    if isinstance(got, dict) and 'probeError' in got:
        raise AssertionError('the probe threw: ' + got['probeError'])
    return got
