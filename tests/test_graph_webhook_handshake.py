"""Graph's subscription handshake must be answered — WP: mailbox ingest.

Microsoft Graph validates a notificationUrl by sending it a request with
?validationToken=… and requires the token echoed back verbatim as
text/plain. It sends that as a POST, not a GET.

The handler only echoed on GET, so every create and renew failed with
"Subscription validation request to notification URL did not return the
expected validation token", and the leads@procamgroup.in subscription
was dead from 2 September.
"""
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'HookTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'hook.db')

import importlib                                              # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
flask_app.config['WTF_CSRF_ENABLED'] = False

TOKEN = 'Validation: Testing unicode 😀 and spaces'


@pytest.fixture(scope='module')
def client():
    with flask_app.app_context():
        db.create_all()
    return flask_app.test_client()


def test_the_handshake_is_answered_on_post(client):
    """This is the exact request Graph sends, and the one that failed."""
    from urllib.parse import quote
    r = client.post(f'/api/email/webhook?validationToken={quote(TOKEN)}')
    assert r.status_code == 200
    assert r.get_data(as_text=True) == TOKEN, 'the token was not echoed back'
    assert r.mimetype == 'text/plain'


def test_the_handshake_is_still_answered_on_get(client):
    from urllib.parse import quote
    r = client.get(f'/api/email/webhook?validationToken={quote(TOKEN)}')
    assert r.status_code == 200
    assert r.get_data(as_text=True) == TOKEN


def test_the_handshake_does_not_depend_on_ingestion_being_active(client,
                                                                monkeypatch):
    """Graph is verifying the endpoint exists. Refusing because ingestion
    is paused would make the subscription impossible to recreate."""
    from email_ingest import service as mail_service
    monkeypatch.setattr(mail_service, 'webhook_should_run', lambda: False)
    r = client.post(f'/api/email/webhook?validationToken=abc123')
    assert r.status_code == 200
    assert r.get_data(as_text=True) == 'abc123'


def test_a_notification_without_a_token_is_not_treated_as_a_handshake(client):
    """A real notification must still go down the notification path."""
    r = client.post('/api/email/webhook', json={'value': []})
    assert r.status_code in (200, 400)
    assert 'validationToken' not in r.get_data(as_text=True)


def test_a_bare_get_is_still_rejected(client):
    assert client.get('/api/email/webhook').status_code == 400


def test_the_token_is_length_capped(client):
    """It is echoed straight back, so it must not be unbounded."""
    r = client.post('/api/email/webhook?validationToken=' + 'A' * 5000)
    assert r.status_code == 200
    assert len(r.get_data(as_text=True)) <= 1024
