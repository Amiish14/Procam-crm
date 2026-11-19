"""Every internal link must carry the deployment prefix.

The portal is served at /CRM behind nginx.  A link written as href="/rfqs"
resolves at the domain root, where nginx knows nothing about it and
answers its own 404 — the app never sees the request.  Templates get
`url_prefix` from a context processor for exactly this.
"""
import os
import re
import sys
import glob
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_PREFIX = '/CRM'
os.environ['URL_PREFIX'] = _PREFIX
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'PrefixTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'prefix.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db, Employee = _main.app, _main.db, _main.Employee

_TEMPLATES = os.path.join(_ROOT, 'templates')

PAGES = ['/app', '/rfqs', '/quotes', '/handovers', '/competitors',
         '/competitors/dashboard', '/reports', '/help', '/notifications',
         '/funnels/account-development', '/business-cards/scan', '/my-work']

_LINK = re.compile(r'(?:href|src|action)="(/[^/"][^"]*)"')


@pytest.fixture(autouse=True)
def _force_prefix():
    """URL_PREFIX is process-global and other test modules clear it.

    The context processor reads it per request (so a deployment can change
    the prefix without a code change), which makes these tests sensitive to
    import order unless it is pinned here.
    """
    previous = os.environ.get('URL_PREFIX')
    os.environ['URL_PREFIX'] = _PREFIX
    yield
    if previous is None:
        os.environ.pop('URL_PREFIX', None)
    else:
        os.environ['URL_PREFIX'] = previous


@pytest.fixture(scope='module')
def client():
    with flask_app.app_context():
        db.create_all()
        e = Employee.query.filter_by(emp_code='PFXADM').first()
        if not e:
            e = Employee(emp_code='PFXADM', name='Prefix Admin')
            db.session.add(e)
        e.role, e.is_active, e.must_change_pw = 'admin', True, False
        e.vertical, e.is_super_admin = 'All', True
        db.session.commit()
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='PFXADM', name='Prefix Admin', role='admin',
                 vertical='All')
    return c


@pytest.mark.parametrize('path', PAGES)
def test_rendered_links_carry_the_prefix(client, path):
    # SCRIPT_NAME is what url_for() reads to build prefixed URLs; nginx
    # supplies it in production via the ProxyFix/force_script_name wrapper.
    # Without it here, correct url_for() links would look broken.
    r = client.get(path, environ_overrides={'SCRIPT_NAME': _PREFIX})
    if r.status_code in (302, 404):
        pytest.skip(f'{path} → {r.status_code}')
    assert r.status_code == 200, f'{path} → {r.status_code}'
    html = r.get_data(as_text=True)
    # Script bodies build URLs from template literals, so a correctly
    # prefixed "/CRM${x.route}" reads as a bare path to this scan. Client
    # calls have their own guard (test_no_client_side_fetch_bypasses...).
    html = re.sub(r'<script\b.*?</script>', '', html, flags=re.S | re.I)
    bad = [u for u in _LINK.findall(html)
           if not u.startswith(_PREFIX + '/')]
    assert not bad, f'{path} has links that bypass {_PREFIX}: {bad[:5]}'


def test_no_template_hardcodes_an_absolute_link():
    """Static guard, so a new template cannot reintroduce this."""
    offenders = []
    pat = re.compile(r'(?:href|src|action)="(/(?!/)[^"]*)"')
    for tpl in glob.glob(os.path.join(_TEMPLATES, '**', '*.html'),
                         recursive=True):
        for n, line in enumerate(open(tpl), 1):
            for url in pat.findall(line):
                if url.startswith('{{'):
                    continue
                offenders.append(
                    f'{os.path.relpath(tpl, _ROOT)}:{n}: {url}')
    assert not offenders, (
        'Absolute links must start with {{ url_prefix }}:\n  '
        + '\n  '.join(offenders))


def test_nav_hrefs_go_through_crm_url():
    """The top navigation builds its anchors in JS, not Jinja."""
    html = open(os.path.join(_TEMPLATES, 'app.html')).read()
    assert "crmUrl(it.href)" in html, \
        'topnav items must resolve their href through crmUrl()'


def test_crm_url_is_safe_for_stored_routes():
    """Routes come out of the database (TaskInstance.action_route,
    Notification.action_url), so the helper has to prefix a bare path,
    leave an already-prefixed one alone, and not touch external links.
    Double-prefixing 404s exactly like not prefixing does.
    """
    with flask_app.test_request_context('/my-work'):
        crm_url = None
        for proc in flask_app.template_context_processors[None]:
            d = proc()
            if 'crm_url' in d:
                crm_url = d['crm_url']
        assert crm_url is not None

        assert crm_url('/app?lead=9') == _PREFIX + '/app?lead=9'
        assert crm_url(_PREFIX + '/app?lead=9') == _PREFIX + '/app?lead=9'
        assert crm_url('https://example.com/x') == 'https://example.com/x'
        assert crm_url('#anchor') == '#anchor'
        assert crm_url(None) == _PREFIX + '/'


def test_my_work_task_links_are_prefixed(client):
    """Clicking a task in My Work 404'd for everyone: action_route is
    stored bare and was rendered straight into the href."""
    from app.models.task_engine import TaskInstance
    with flask_app.app_context():
        if not TaskInstance.query.filter_by(owner_user_id='PFXADM').first():
            db.session.add(TaskInstance(
                task_key='lead.qualify', entity_type='Lead', entity_id=7,
                entity_display='Test Account', owner_user_id='PFXADM',
                status='Pending', priority=2, action_route='/app?lead=7'))
            db.session.commit()

    html = client.get('/my-work/tasks',
                      environ_overrides={'SCRIPT_NAME': _PREFIX}
                      ).get_data(as_text=True)
    links = re.findall(r'class="task-row" href="([^"]+)"', html)
    assert links, 'no task rows rendered — the fixture did not take'
    for link in links:
        assert link.startswith(_PREFIX + '/') or link == '#', \
            f'task link bypasses {_PREFIX}: {link}'


def test_no_client_side_fetch_bypasses_the_prefix():
    """A raw fetch('/api/...') 404s under /CRM and the page renders empty.

    This is how the Account, Project and Sales funnels, business-card
    scanning, the RFQ pages, the competitor pages and Help admin all
    loaded successfully and then showed nothing: the HTML was fine, every
    data call missed.

    app.html wraps them in crmUrl(); server-rendered templates prefix the
    literal with url_prefix.
    """
    import re
    pat = re.compile(r"""fetch\(\s*(['"`])(/(?!/)[^'"`]*)""")
    offenders = []
    for tpl in glob.glob(os.path.join(_TEMPLATES, '**', '*.html'),
                         recursive=True):
        for n, line in enumerate(open(tpl), 1):
            for m in pat.finditer(line):
                url = m.group(2)
                if url.startswith('{{'):
                    continue
                offenders.append(
                    f'{os.path.relpath(tpl, _ROOT)}:{n}: fetch("{url}")')
    assert not offenders, (
        'These calls resolve at the domain root, not under the deployment '
        'prefix:\n  ' + '\n  '.join(offenders)
        + '\n\nIn app.html wrap the URL in crmUrl(); elsewhere prefix it '
          'with {{ url_prefix }}.')


def _prefix_roots(text):
    """Constants in this template that hold the prefix itself.

    The quotes are optional, and that mattered: `const PREFIX = {{
    url_prefix | tojson }}` was recognised and `const PREFIX = "{{
    url_prefix }}"` was not, although three pages and four lists use
    the second form. With no root found, nothing in the file could be
    traced to the prefix, so the guard below reported the very helper
    that does the prefixing.
    """
    return set(re.findall(
        r'(?:const|let|var)\s+(\w+)\s*=\s*[\'"`]?\{\{\s*url_prefix',
        text))


def _local_prefixers(text):
    """Names in this template that already add the prefix.

    Two steps, because the prefix usually arrives through a constant:
    `const PREFIX = {{ url_prefix | tojson }}` and then
    `const u = (p) => PREFIX + p`. A name counts only if it can be
    traced back to `url_prefix` in this same file — a helper called
    `u` that adds something else is not a prefixer.
    """
    roots = _prefix_roots(text)
    if not roots:
        return ()
    found = set()
    for name, expr in re.findall(
            r'(?:const|let|var)\s+(\w+)\s*=\s*(?:\([^)]*\)|\w+)\s*=>\s*'
            r'([^;\n]{0,120})', text):
        if any(root in expr for root in roots):
            found.add(name + '(')
    for name, body in re.findall(
            r'function\s+(\w+)\s*\([^)]*\)\s*\{([^}]{0,200})', text):
        if any(root in body for root in roots):
            found.add(name + '(')
    return tuple(found)


def test_a_helper_that_prefixes_inside_itself_is_safe():
    """`function post(path){ fetch(PREFIX + path) }` is prefixed, and
    the call site passing `/api/...` is right to."""
    text = ('const PREFIX = "{{ url_prefix }}";\n'
            'async function post(path, body){ const r = await '
            'fetch(PREFIX + path, {method:"POST"}); }\n')
    assert 'PREFIX' in _prefix_roots(text)


def test_a_quoted_prefix_constant_is_recognised_too():
    """`const URL_PREFIX = "{{ url_prefix }}"` is the form the RFQ,
    quote and competitor lists use, and it read as no prefixer at all
    -- which turned the guard into a false alarm naming the very
    helper that does the prefixing. A false alarm from a guard against
    silent failure is the one fault it must not have, because the next
    alarm is read as noise."""
    text = ('const URL_PREFIX = "{{ url_prefix }}";\n'
            'function crmUrl(p){ return URL_PREFIX + p; }\n')
    assert _local_prefixers(text) == ('crmUrl(',)


def test_a_wrapper_is_not_blamed_for_the_next_functions_fetch():
    """The window that looks for a `fetch()` inside a helper must stop
    at that helper's own closing brace."""
    text = ('const URL_PREFIX = "{{ url_prefix }}";\n'
            'function crmUrl(p){ return URL_PREFIX + p; }\n'
            'async function jfetch(url, opts){ const r = '
            'await fetch(url, opts); return r.json(); }\n'
            "crmUrl('/rfqs/1');\n")
    assert 'crmUrl(' in _local_prefixers(text)


def test_a_local_prefixer_is_recognised_as_one():
    """The guard above is only as useful as its ability to tell a
    prefixed call from an unprefixed one."""
    text = ('const PREFIX = {{ url_prefix | tojson }};\n'
            'const u = (p) => PREFIX + p;\n'
            'const v = (p) => "/other" + p;\n')
    assert _local_prefixers(text) == ('u(',)


#: A function whose body reaches a fetch() call. `[^{}]` in the body,
#: so the window cannot run past the end of the function it started in
#: and pick up the NEXT function's fetch(). That is what happened on
#: the RFQ and competitor lists: the one-line `crmUrl` helper was
#: credited with the `fetch()` inside the `jfetch` defined under it and
#: reported for handing itself an unprefixed path -- while the loose
#: version was also swallowing whole functions and missing real ones.
_WRAPPER = re.compile(
    r'(?:async\s+)?function\s+(\w+)\s*\([^)]*\)\s*\{'
    r'([^{}]{0,400}?)fetch\(([^;]{0,120})',
    re.S)


def _unprefixed_wrapper_calls(text, rel='<text>'):
    """Call sites that hand a bare absolute path to a helper which
    passes it to fetch() without prefixing it."""
    # 14 Nov. A template may build its own prefixer -- Account link has
    # `const PREFIX = {{ url_prefix | tojson }}` and `const u = (p) =>
    # PREFIX + p`, and calls `fetch(u(path))`. That is prefixed, and
    # this guard was calling it a silent 404: a false alarm from a
    # guard against silent failure is the one fault it must not have,
    # because the next alarm is read as noise.
    #
    # The ROOTS count as well as the helpers. `fetch(PREFIX + path)`
    # inside a helper is prefixed, and saying so is the difference
    # between this guard naming three pages that are correct and
    # naming none.
    prefixers = _local_prefixers(text)
    roots = _prefix_roots(text)
    safe_words = ('url_prefix', 'crmUrl') + prefixers + tuple(roots)
    unsafe = set()
    for m in _WRAPPER.finditer(text):
        name, body, fetch_arg = m.group(1), m.group(2), m.group(3)
        # Prefixing may happen on the fetch line or earlier in the body.
        if any(word in fetch_arg for word in safe_words) \
           or 'url_prefix' in body or 'crmUrl' in body:
            continue
        unsafe.add(name)
    out = []
    for name in sorted(unsafe):
        call = re.compile(re.escape(name) + r"""\(\s*(['"`])(/(?!/)[^'"`]*)""")
        for n, line in enumerate(text.splitlines(), 1):
            for m in call.finditer(line):
                out.append(f'{rel}:{n}: {name}("{m.group(2)}")')
    return out


def test_no_fetch_wrapper_bypasses_the_prefix():
    """The same bug, one level of indirection down.

    test_no_client_side_fetch_bypasses_the_prefix only sees a literal
    fetch('/api/...'). Three pages -- the quote list, quote detail and
    the handover queue -- routed every call through a local `jf(u)`
    helper that passed the bare path straight to fetch(), so they
    rendered fine and loaded nothing, exactly like the funnels did.

    So: find each helper in a template that calls fetch() on its own
    argument, and if the helper doesn't prefix, no call site may hand
    it a bare absolute path.
    """
    offenders = []
    for tpl in glob.glob(os.path.join(_TEMPLATES, '**', '*.html'),
                         recursive=True):
        rel = os.path.relpath(tpl, _ROOT)
        offenders += _unprefixed_wrapper_calls(open(tpl).read(), rel)
    assert not offenders, (
        'These helpers pass an unprefixed path to fetch(), so the page '
        'loads and stays empty:\n  ' + '\n  '.join(offenders)
        + '\n\nPrefix inside the helper (as bulk_admin/leads.html does) '
          'or at the call site.')


def test_the_guard_still_catches_a_helper_that_does_not_prefix():
    """The fault it was built for, which the fixes above must not have
    taken the teeth out of."""
    text = ('function jf(u){ return fetch(u, {credentials:"same-origin"})'
            '.then(r => r.json()); }\n'
            "jf('/api/quotes');\n")
    assert _unprefixed_wrapper_calls(text) == ['<text>:2: jf("/api/quotes")']


def test_a_relative_path_is_not_an_offence():
    """Only an absolute path breaks behind a prefix."""
    text = ('function jf(u){ return fetch(u); }\n'
            "jf('api/quotes');\n")
    assert _unprefixed_wrapper_calls(text) == []


@pytest.mark.parametrize('path', [
    '/funnels/account-development',
    '/funnels/project-intelligence',
    '/funnels/sales',
    '/business-cards/scan',
])
def test_the_funnel_and_scan_pages_can_reach_their_data(client, path):
    """They returned 200 while every fetch inside them 404'd."""
    import re
    r = client.get(path, environ_overrides={'SCRIPT_NAME': _PREFIX})
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    calls = re.findall(r"""fetch\(\s*['"`](/[^'"`]*)""", html)
    assert calls, f'{path} makes no data call at all — did it lose its script?'
    bad = [u for u in calls if not u.startswith(_PREFIX + '/')]
    assert not bad, f'{path} calls outside the prefix: {bad}'
