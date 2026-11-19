"""Every in-app navigation carries the prefix the CRM is served under.

UI sweep 19 Nov item 2: clicking RFQ-2026-0001 went to `/rfqs/1`, which
nginx answers with a 404. The CRM is mounted under a prefix and four
pages built their paths without it. The stylesheet links on the very
same pages had it all along, which is why nothing else looked wrong and
why this survived a visual sweep.

The faults were all `location = '/…'` in JavaScript, never an `href`:
Jinja fills an href when the page is rendered, and these were assembled
in the browser where `url_prefix` is not in scope unless the page puts
it there.
"""
import glob
import os
import re
import sys

os.environ.setdefault('SECRET_KEY', 'test-only-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Test-123456789')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: `location = '/x'`, `location.href = "/x"`, backticks too.
BARE_NAV = re.compile(r"""location(?:\.href)?\s*=\s*['"`]/[a-zA-Z]""")

#: A path that is NOT the CRM's own: an absolute URL, or the few roots
#: nginx serves itself.
ALLOWED_BARE = ('/static/', '/favicon')


def _templates():
    return sorted(glob.glob(os.path.join(ROOT, 'templates', '**', '*.html'),
                            recursive=True))


def test_no_page_navigates_to_an_unprefixed_path():
    bad = []
    for path in _templates():
        body = open(path).read()
        for hit in BARE_NAV.finditer(body):
            tail = body[hit.start():hit.start() + 80]
            if any(ok in tail for ok in ALLOWED_BARE):
                continue
            line = body[:hit.start()].count('\n') + 1
            bad.append(f'{os.path.relpath(path, ROOT)}:{line}  {tail.strip()[:60]}')
    assert bad == [], (
        'these would 404 behind the prefix:\n  ' + '\n  '.join(bad))


def test_the_four_pages_that_were_wrong_now_use_crmurl():
    for name, paths in {
        'rfq/list.html': ('/rfqs/',),
        'quote/list.html': ('/quotes/',),
        'quote/detail.html': ('/quotes/',),
        'competitor/master.html': ('/competitors/',),
    }.items():
        body = open(os.path.join(ROOT, 'templates', name)).read()
        assert 'function crmUrl(' in body, name
        assert 'const URL_PREFIX = "{{ url_prefix }}"' in body, name
        for said in paths:
            assert f"crmUrl('{said}" in body, (name, said)


def test_a_row_click_still_interpolates_the_id():
    """The row handlers sit inside a template literal, so the id has to
    stay `${r.id}`. Writing `+ r.id` there put a runtime reference in an
    onclick attribute where `r` does not exist -- it looked right and
    every row would have thrown."""
    for name, needle in (
        ('rfq/list.html', "location=crmUrl('/rfqs/${r.id}')"),
        ('quote/list.html', "location=crmUrl('/quotes/${q.id}')"),
        ('competitor/master.html', "location=crmUrl('/competitors/${r.id}')"),
    ):
        body = open(os.path.join(ROOT, 'templates', name)).read()
        assert needle in body, (name, needle)


def test_the_helper_is_defined_before_anything_calls_it():
    for name in ('rfq/list.html', 'quote/list.html', 'quote/detail.html',
                 'competitor/master.html'):
        body = open(os.path.join(ROOT, 'templates', name)).read()
        assert body.index('function crmUrl(') < body.index('crmUrl(\'/'), name


def test_the_api_calls_on_those_pages_were_already_prefixed():
    """They were, which is the point: only the navigations were wrong,
    so the fix is narrow and this says so."""
    for name in ('rfq/list.html', 'quote/list.html', 'quote/detail.html',
                 'competitor/master.html'):
        body = open(os.path.join(ROOT, 'templates', name)).read()
        for hit in re.finditer(r"""fetch\(\s*['"`]/[a-zA-Z]""", body):
            tail = body[hit.start():hit.start() + 60]
            assert any(ok in tail for ok in ALLOWED_BARE), (name, tail)
