"""The CRM must never email a customer.

Everything the CRM sends is meant for a colleague, and some of it now
carries a client's own documents. The check lives in front of the one
transport, so these tests exercise it there as well as on its own: a
guard that is only unit-tested is a guard somebody routes around.
"""
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'mail-policy-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'MailPolicyTest12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'mailpolicy.db'))

from app.services import mail_policy                      # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv('CRM_INTERNAL_EMAIL_DOMAINS', raising=False)
    monkeypatch.delenv('CRM_EMAIL_ALLOW_EXTERNAL', raising=False)


@pytest.mark.parametrize('address', [
    'someone@procamlogistics.com',
    'Someone@ProcamLogistics.COM',
    'ops@procamgroup.in',
    'a.b@mail.procamgroup.in',            # a subdomain of an allowed domain
    'Name Surname <ops@procamgroup.in>',  # a display name must not hide it
])
def test_internal_addresses_are_allowed(address):
    assert mail_policy.is_internal(address)


@pytest.mark.parametrize('address', [
    'buyer@customer.com',
    'someone@gmail.com',
    'ops@procamgroup.in.evil.com',        # the allowed domain as a prefix
    'ops@notprocamgroup.in',              # the allowed domain as a suffix
    'nodomain',
    'two@@procamgroup.in',
    '',
])
def test_everything_else_is_refused(address):
    assert not mail_policy.is_internal(address)


def test_check_splits_and_keeps_the_refused_addresses():
    allowed, blocked = mail_policy.check(
        ['ops@procamgroup.in', 'buyer@customer.com', ' '])
    assert allowed == ['ops@procamgroup.in']
    assert blocked == ['buyer@customer.com']


def test_an_extra_domain_can_be_configured(monkeypatch):
    monkeypatch.setenv('CRM_INTERNAL_EMAIL_DOMAINS', 'procam.co.uk')
    assert mail_policy.is_internal('a@procam.co.uk')
    assert not mail_policy.is_internal('a@elsewhere.co.uk')


def test_the_block_can_be_lifted_deliberately(monkeypatch):
    assert not mail_policy.is_internal('buyer@customer.com')
    monkeypatch.setenv('CRM_EMAIL_ALLOW_EXTERNAL', 'true')
    allowed, blocked = mail_policy.check(['buyer@customer.com'])
    assert allowed == ['buyer@customer.com'] and not blocked


# ── at the transport ─────────────────────────────────────────────────
def test_the_transport_refuses_an_external_recipient(monkeypatch):
    """The whole point: no call site can route around this."""
    from email_ingest import notifier

    import email_ingest.graph_client as gc

    sent = []
    monkeypatch.setattr(notifier, 'is_enabled', lambda: True)
    monkeypatch.setattr(notifier, 'sender', lambda: 'leads@procamgroup.in')

    class _Boom:
        def __init__(self, *a, **k):
            sent.append(True)
            raise AssertionError('the transport tried to send externally')

    monkeypatch.setattr(gc, 'GraphClient', _Boom)

    assert notifier.send('buyer@customer.com', 'Quote', '<p>hi</p>') is False
    assert not sent


def test_the_transport_drops_only_the_external_half(monkeypatch):
    from email_ingest import notifier
    import email_ingest.graph_client as gc

    captured = {}

    class _Fake:
        def _request(self, method, path, json_body=None, **kw):
            captured['to'] = [r['emailAddress']['address']
                              for r in json_body['message']['toRecipients']]

            class R:
                status_code = 202
                text = ''
            return R()

    monkeypatch.setattr(notifier, 'is_enabled', lambda: True)
    monkeypatch.setattr(notifier, 'sender', lambda: 'leads@procamgroup.in')
    monkeypatch.setattr(gc, 'GraphClient', _Fake)

    ok = notifier.send(['ops@procamgroup.in', 'buyer@customer.com'],
                       'Subject', '<p>body</p>')
    assert ok is True
    assert captured['to'] == ['ops@procamgroup.in']
