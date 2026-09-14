"""The browser is told what the CRM may load, and that is all it may load."""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'HeadersTestOnly12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'headers.db'))

from app import app as flask_app                                # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _csp():
    r = flask_app.test_client().get('/login')
    return r, r.headers.get('Content-Security-Policy') or ''


def test_the_policy_is_enforced_not_only_reported():
    r, csp = _csp()
    assert csp, 'Content-Security-Policy must be enforced'
    for directive in ("object-src 'none'", "base-uri 'self'",
                      "form-action 'self'", "frame-ancestors 'none'"):
        assert directive in csp


def test_every_external_origin_the_templates_use_is_allowed():
    """If a template loads from an origin the policy does not list, the
    page breaks in production. Scan them all."""
    _r, csp = _csp()
    origins = set()
    for dirpath, _dirs, files in os.walk(os.path.join(_ROOT, 'templates')):
        for f in files:
            text = open(os.path.join(dirpath, f), encoding='utf-8').read()
            origins |= set(re.findall(
                r'(?:src|href)="(https://[^"/]+)', text))
    for origin in origins:
        assert origin in csp, f'{origin} is used by a template but blocked'


def test_other_headers():
    r, _csp_ = _csp()
    assert r.headers.get('X-Frame-Options') == 'DENY'
    assert r.headers.get('X-Content-Type-Options') == 'nosniff'
    assert 'camera=()' in (r.headers.get('Permissions-Policy') or '')
    assert r.headers.get('Cross-Origin-Opener-Policy') == 'same-origin'


def test_api_answers_are_not_cached():
    r = flask_app.test_client().get('/api/leads')
    assert r.headers.get('Cache-Control') == 'no-store'


def test_copilot_questions_are_rate_limited_per_person():
    import app as app_module
    from app import db, Employee
    flask_app.config['WTF_CSRF_ENABLED'] = False
    with flask_app.app_context():
        db.create_all()
        e = Employee.query.filter_by(emp_code='RLUSER').first()
        if e is None:
            e = Employee(emp_code='RLUSER', name='RLUSER')
            db.session.add(e)
        e.role, e.is_active, e.must_change_pw, e.session_version = \
            'user', True, False, 0
        db.session.commit()
    before = app_module.limiter.enabled
    app_module.limiter.enabled = True
    app_module.limiter.reset()
    try:
        c = flask_app.test_client()
        with c.session_transaction() as s:
            s.update(emp_code='RLUSER', name='x', role='user', vertical='All')
        codes = [c.post('/api/copilot/ask', json={'q': 'my open leads'})
                 .status_code for _ in range(45)]
        assert 429 in codes
        assert codes[:40].count(429) == 0
    finally:
        app_module.limiter.reset()
        app_module.limiter.enabled = before


def test_the_service_worker_leaves_other_origins_alone():
    """Its fetch() runs under its own CSP (connect-src 'self'); handling
    cross-origin requests there breaks CDN scripts, fonts and the logo."""
    src = open(os.path.join(_ROOT, 'static', 'sw.js')).read()
    handler = src[src.index("addEventListener('fetch'"):]
    assert 'self.location.origin' in handler
    assert handler.index('self.location.origin') < handler.index('respondWith')
