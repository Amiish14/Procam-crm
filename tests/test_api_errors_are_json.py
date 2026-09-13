"""
Errors under /api answer in JSON, and the Academy self-checks work.

Why this module exists separately: every other test suite sets
`WTF_CSRF_ENABLED = False`, which is sensible for testing a handler and
is exactly why nobody noticed that the Academy page never sent a token.
CSRFProtect rejected each POST with a 400 and an HTML error page, the
front-end called .json() on it, the promise rejected, and the button did
nothing. The handler was fine the whole time and every test of it passed.

So CSRF stays ON here. These tests exercise the path a browser takes.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'ApiJsonTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'apijson.db')

from app import app as flask_app, db, Employee            # noqa: E402

@pytest.fixture(autouse=True)
def csrf_on():
    """CSRF enabled for these tests, and only these.

    Set per test rather than at import: the Flask app is a singleton
    shared by every test module, and the others disable CSRF at import
    time — so whichever module imports last decides, and in a full run
    that is not this one. Restored afterwards so nothing else changes
    behaviour depending on collection order.
    """
    app_cfg = flask_app.config
    before = (app_cfg.get('WTF_CSRF_ENABLED'),
              app_cfg.get('WTF_CSRF_SSL_STRICT'))
    app_cfg['WTF_CSRF_ENABLED'] = True
    app_cfg['WTF_CSRF_SSL_STRICT'] = False
    yield
    app_cfg['WTF_CSRF_ENABLED'], app_cfg['WTF_CSRF_SSL_STRICT'] = before


@pytest.fixture()
def user():
    with flask_app.app_context():
        db.create_all()
        e = Employee.query.filter_by(emp_code='AJUSER').first()
        if e is None:
            e = Employee(emp_code='AJUSER', name='Api Json User')
            db.session.add(e)
        e.role, e.is_active, e.must_change_pw = 'user', True, False
        e.vertical, e.is_super_admin = 'All', False
        db.session.commit()
        return 'AJUSER'


def _client(code='AJUSER'):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role='user', vertical='All')
    return c


def _token(client):
    """The token a browser would read out of the cookie."""
    client.get('/academy')                       # any page sets it
    for cookie in client.cookie_jar if hasattr(client, 'cookie_jar') else []:
        if cookie.name == 'csrf_token':
            return cookie.value
    # Werkzeug 2.3+ moved the jar; fall back to the response header.
    r = client.get('/academy')
    for header in r.headers.getlist('Set-Cookie'):
        if header.startswith('csrf_token='):
            return header.split('=', 1)[1].split(';')[0]
    return ''


# ── the guard: no HTML under /api, ever ──────────────────────────────
def test_a_post_without_a_token_answers_json_not_html(user):
    """The exact failure. Before the fix this was a 400 whose body began
    "<!doctype", which is what threw "Unexpected token '<'"."""
    r = _client().post('/api/academy/basics/practice', json={'answer': '1'})
    assert r.status_code == 400
    assert r.mimetype == 'application/json', r.get_data(as_text=True)[:80]
    body = r.get_json()
    assert body['ok'] is False
    assert body['code'] == 'csrf'
    assert 'refresh' in body['error'].lower()


def test_no_api_error_response_is_ever_html(user):
    """Swept across the shapes a client can get wrong."""
    c = _client()
    for path, payload in (
            ('/api/academy/basics/practice', {'answer': '1'}),
            ('/api/academy/basics/quiz', {'answers': ['a']}),
            ('/api/academy/nosuchlevel/practice', {'answer': '1'}),
            ('/api/academy/basics/learned', {}),
    ):
        r = c.post(path, json=payload)
        assert r.mimetype == 'application/json', f'{path} returned HTML'
        assert not r.get_data(as_text=True).lstrip().startswith('<')


def test_an_unknown_api_path_is_json_too(user):
    r = _client().post('/api/academy/basics/nosuchthing', json={})
    assert r.status_code == 404
    assert r.mimetype == 'application/json'
    assert r.get_json()['ok'] is False


def test_pages_still_render_html(user):
    """The handler must not turn the whole portal into an API. An
    ordinary page's 404 is still a page."""
    r = _client().get('/no-such-page-at-all')
    assert r.status_code == 404
    assert r.mimetype != 'application/json'


# ── the fix: with a token, the self-checks work ──────────────────────
def _post(c, path, payload):
    return c.post(path, json=payload,
                  headers={'X-CSRFToken': _token(c),
                           'X-Requested-With': 'XMLHttpRequest'})


def test_practice_returns_a_verdict(user):
    c = _client()
    r = _post(c, '/api/academy/basics/practice', {'answer': '1'})
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] is True
    assert 'correct' in body


def test_quiz_returns_a_score(user):
    from app.training.content import BY_KEY

    c = _client()
    questions = BY_KEY['basics']['quiz']
    # The right answers, so a full score proves grading ran rather than
    # merely that the endpoint replied.
    answers = [str(correct) for _text, _opts, correct in questions]
    r = _post(c, '/api/academy/basics/quiz', {'answers': answers})
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] is True
    assert body['score'] == 100, body

    # and the wrong ones score less, so it is not returning 100 blindly
    wrong = [str((correct + 1) % len(opts))
             for _text, opts, correct in questions]
    body = _post(c, '/api/academy/basics/quiz',
                 {'answers': wrong}).get_json()
    assert body['score'] < 100, body


def test_learned_works_too(user):
    """The same broken helper, and the brief only listed two of three."""
    c = _client()
    r = _post(c, '/api/academy/basics/learned', {})
    assert r.status_code == 200
    assert r.get_json()['ok'] is True


# ── bad input is a message, not a dead button ────────────────────────
def test_an_empty_body_is_answered_in_json(user):
    c = _client()
    r = c.post('/api/academy/basics/practice',
               data='', content_type='application/json',
               headers={'X-CSRFToken': _token(c)})
    assert r.mimetype == 'application/json'
    assert r.status_code in (200, 400)


def test_garbage_body_is_answered_in_json(user):
    c = _client()
    r = c.post('/api/academy/basics/practice',
               data='not json at all', content_type='application/json',
               headers={'X-CSRFToken': _token(c)})
    assert r.mimetype == 'application/json'
    assert not r.get_data(as_text=True).lstrip().startswith('<')


def test_an_unknown_level_is_a_json_404(user):
    c = _client()
    r = _post(c, '/api/academy/nosuchlevel/practice', {'answer': '1'})
    assert r.status_code == 404
    assert r.get_json()['ok'] is False


# ── the front-end holds up its end ───────────────────────────────────
def test_the_page_sends_the_csrf_header(user):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'templates', 'training',
                           'level.html')) as fh:
        src = fh.read()
    assert 'X-CSRFToken' in src
    assert 'csrf_token' in src


def test_the_page_checks_the_content_type_before_parsing(user):
    """Guards against the symptom returning by another route: if
    anything ever answers with HTML again, the page must say so rather
    than throw."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'templates', 'training',
                           'level.html')) as fh:
        src = fh.read()
    assert 'content-type' in src
    assert 'application/json' in src
    assert 'catch' in src


def test_the_page_reenables_its_button_on_failure(user):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'templates', 'training',
                           'level.html')) as fh:
        src = fh.read()
    assert 'btn.disabled = false' in src


# ── an expired session is the other way to get "Unexpected token '<'" ──
def test_an_unauthenticated_self_check_answers_json_not_a_login_page(user):
    """The report's acceptance criteria name this case and nothing tested it.

    A trainee who leaves the lesson open past their session and then
    clicks "Check my answer" is not sending a bad token — they are not
    logged in. If that redirected to the login page, the front-end would
    parse HTML again and show the exact symptom this module was written
    to end. It must be a JSON 401.
    """
    c = flask_app.test_client()                  # no session at all
    token = _token(c)
    for path, body in (('/api/academy/basics/practice', {'answer': 3}),
                       ('/api/academy/basics/quiz', {'answers': {}})):
        r = c.post(path, json=body, headers={'X-CSRFToken': token})
        assert r.status_code == 401, f'{path} → {r.status_code}'
        assert r.is_json, f'{path} answered {r.content_type}, not JSON'
        assert r.get_json()['ok'] is False
        assert not r.get_data(as_text=True).lstrip().startswith('<')
