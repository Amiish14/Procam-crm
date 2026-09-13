"""
Without a clientState secret the webhook refuses every notification.

The secret is the only proof a POST to /api/email/webhook came from our
Graph subscription. The handler used to skip the check when the secret
was unset, so on a misconfigured server anyone could post a notification
naming a message and have it ingested.
"""
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.modules.setdefault('msal', types.ModuleType('msal'))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'WebhookTestOnly12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'webhook.db'))

from email_ingest import webhook as W                         # noqa: E402

NOTE = {'value': [{'clientState': 'anything', 'changeType': 'created',
                   'resource': "Users('leads@procamgroup.in')/Messages('x')"}]}


class Boom:
    def __init__(self, *a, **k):
        raise AssertionError('Graph must not be contacted')


def test_no_secret_refuses_before_touching_graph(monkeypatch):
    monkeypatch.delenv('EMAIL_WEBHOOK_SECRET', raising=False)
    monkeypatch.setattr(W, 'GraphClient', Boom)
    stats = W.handle_notification(NOTE)
    assert stats['processed'] == 0
    assert stats['refused'] == 1
    assert 'EMAIL_WEBHOOK_SECRET' in stats['error']


def test_an_empty_client_state_never_matches(monkeypatch):
    """compare_digest('', '') is True — so the per-item check must not be
    the only guard. With the secret set, a notification with no
    clientState is rejected."""
    monkeypatch.setenv('EMAIL_WEBHOOK_SECRET', 'the-real-secret')

    class Quiet:
        def __init__(self, *a, **k):
            pass
    monkeypatch.setattr(W, 'GraphClient', Quiet)
    from app import app as flask_app, db
    with flask_app.app_context():
        db.create_all()
    stats = W.handle_notification(
        {'value': [{'clientState': '', 'resource': 'x'}]})
    assert stats['processed'] == 0
    assert stats['failed'] == 1
