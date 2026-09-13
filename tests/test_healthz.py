"""/healthz answers without a login, and says nothing but up or down."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'HealthzTestOnly12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'healthz.db'))

from app import app as flask_app, db                           # noqa: E402


def test_up_without_a_login():
    r = flask_app.test_client().get('/healthz')
    assert r.status_code == 200
    assert r.get_json() == {'ok': True}


def test_a_database_failure_is_a_503(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError('database gone')
    monkeypatch.setattr(db.session, 'execute', boom)
    r = flask_app.test_client().get('/healthz')
    assert r.status_code == 503
    assert r.get_json() == {'ok': False}
