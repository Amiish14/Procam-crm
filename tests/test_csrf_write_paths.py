"""
Every write from a page must carry the CSRF token.

This is the fifth time a button has shipped dead on this project for
the same reason, so it gets a test rather than another fix.

The failure. `CSRFProtect` rejects a POST without a token with a 400
*before the view runs*. The page gets an error it was not written to
expect, the handler never executes, and the button appears to do
nothing at all — no error, no change, no log line. It is invisible in
review because the markup is right and the route is right.

Why the rest of the suite cannot see it. Every test module sets
`WTF_CSRF_ENABLED = False`, because otherwise every POST in every test
would need a token. That is the correct trade for testing behaviour,
and it means behaviour tests are structurally blind to this. So this
file reads the templates instead.

The rule. A page may write in one of two ways:

  * through a global `window.fetch` wrapper that adds the header —
    `app.html` does this, and everything served inside the app shell
    inherits it;
  * through a named helper on that page which sets `X-CSRFToken`.

A bare `fetch(..., {method: 'POST'})` with no token in sight is the
bug, and is what this fails on.
"""
import glob
import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = os.path.join(_ROOT, 'templates')

#: Methods that CSRFProtect guards.
WRITE = ('POST', 'PUT', 'PATCH', 'DELETE')

#: How far after a `method:` to look for the token.
WINDOW = 700

_METHOD = re.compile(
    r"""method\s*:\s*['"]?(POST|PUT|PATCH|DELETE)['"]?""", re.I)

#: A call written as `fetch(` rather than through a helper. A helper
#: that sets the header once is the right shape and is checked by the
#: page-level rule below; a bare call has to carry it itself.
_BARE = re.compile(r'(?<![\w.])fetch\s*\(')


#: The shared wrapper, included by standalone pages.
WRAPPER_INCLUDE = '_csrf_fetch.html'


def _wraps_fetch_globally(src):
    """The page replaces window.fetch and adds the header there,
    either inline or by including the shared partial."""
    if WRAPPER_INCLUDE in src:
        return True
    return ('window.fetch' in src) and 'X-CSRFToken' in src


def _is_full_page(src):
    """A partial is rendered inside another page and inherits its
    wrapper, so it is not judged on its own."""
    head = src[:400].lower()
    return '<!doctype' in head or '<html' in head


def _templates():
    for path in sorted(glob.glob(os.path.join(_TEMPLATES, '**', '*.html'),
                                 recursive=True)):
        with open(path, encoding='utf-8', errors='ignore') as fh:
            yield path, fh.read()


#: Pages that were writing without a token when this guard was
#: written, on 19 October. All six were fixed in the same push by
#: including `_csrf_fetch.html`, so the list is empty — and it stays
#: a list rather than being deleted, because the next page to break
#: this rule should have somewhere obvious to be recorded while it is
#: being fixed, not a reason to weaken the rule.
#:
#: A name here that is no longer an offender fails the suite, so the
#: list cannot quietly outlive the bugs it describes.
KNOWN_UNFIXED = ()


def _offenders():
    """Two rules.

    1. A page that writes and contains no token anywhere. Nothing on
       it can possibly be sending one.
    2. A bare `fetch(` with a write method and no token in its own
       options, on a full page with no global wrapper. This is the
       one that bit today: the page had a correct `postJson` helper
       and the new button did not use it, so rule 1 was satisfied
       while the button was still dead.
    """
    out = []
    for path, src in _templates():
        rel = os.path.relpath(path, _ROOT)
        if 'fetch(' not in src:
            continue
        writes = list(_METHOD.finditer(src))
        if not writes:
            continue
        has_token = 'X-CSRFToken' in src or 'X-CSRF-Token' in src
        if _wraps_fetch_globally(src):
            continue
        if not has_token:
            out.append(f'{rel}: writes, and sends no CSRF token anywhere')
            continue
        if not _is_full_page(src):
            continue                 # a partial inherits its parent
        for m in writes:
            window = src[m.start():m.start() + WINDOW]
            if 'X-CSRFToken' in window or 'X-CSRF-Token' in window:
                continue
            before = src[max(0, m.start() - 400):m.start()]
            if not _BARE.search(before):
                continue             # through a helper, covered above
            line = src[:m.start()].count('\n') + 1
            snippet = ' '.join(src[max(0, m.start() - 90):
                                   m.start() + 60].split())
            out.append(f'{rel}:{line}  bare fetch, no token  …{snippet}…')
    return out


def test_no_page_writes_without_a_csrf_token():
    bad = [b for b in _offenders()
           if b.split(':')[0] not in KNOWN_UNFIXED]
    assert not bad, (
        'These writes send no CSRF token, so CSRFProtect rejects them '
        'with a 400 before the view runs and the button silently does '
        'nothing:\n  ' + '\n  '.join(bad))


def test_the_guard_can_actually_fail():
    """A lint that cannot fail is decoration.

    The checker is run against a page that writes without a token, to
    prove it sees one. Five dead buttons shipped under a green suite;
    a guard nobody has watched fail is not evidence of anything.
    """
    import tempfile
    bad_page = (
        "<!DOCTYPE html><html><body><script>\n"
        "fetch('/api/thing', {method: 'POST', "
        "headers: {'Content-Type': 'application/json'}});\n"
        "</script></body></html>\n")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'bad.html')
        with open(path, 'w') as fh:
            fh.write(bad_page)
        # Point the scanner at the fake tree.
        global _TEMPLATES
        real = _TEMPLATES
        try:
            _TEMPLATES = tmp
            assert _offenders(), 'the guard did not notice a bare POST'
        finally:
            _TEMPLATES = real

    # And that a tokened write is accepted.
    good_page = (
        "<!DOCTYPE html><html><body><script>\n"
        "fetch('/api/thing', {method: 'POST', "
        "headers: {'Content-Type': 'application/json', "
        "'X-CSRFToken': t}});\n"
        "</script></body></html>\n")
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, 'good.html'), 'w') as fh:
            fh.write(good_page)
        real = _TEMPLATES
        try:
            _TEMPLATES = tmp
            assert _offenders() == [], 'the guard flagged a correct write'
        finally:
            _TEMPLATES = real


def test_the_pages_touched_today_are_covered():
    """Named explicitly, so a rename cannot quietly drop them from
    the scan and leave the rule passing over nothing."""
    for name in ('workbench/home.html', 'competitor/list.html',
                 'app.html'):
        assert os.path.exists(os.path.join(_TEMPLATES, name)), name


def test_the_quarantine_list_does_not_outlive_the_bugs():
    """A name on KNOWN_UNFIXED that is no longer broken must come off.

    Otherwise the list becomes a place where a page is permanently
    exempt from the rule, which is how the rule stops being one.
    """
    still_bad = {b.split(':')[0] for b in _offenders()}
    stale = [n for n in KNOWN_UNFIXED if n not in still_bad]
    assert not stale, (
        'These are no longer writing without a token — take them off '
        'KNOWN_UNFIXED:\n  ' + '\n  '.join(stale))
