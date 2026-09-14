"""Graph re-delivers a notification the webhook answers slowly. A message
must be classified once: a second pass repeated the AI call, filed a
second review-queue row and could reach a different verdict."""
import os
import sys
import tempfile
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.modules.setdefault('msal', types.ModuleType('msal'))

os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'RedeliveryTest12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'redelivery.db')
os.environ['LEAD_INTAKE_MODE'] = 'enforce'
os.environ['LEAD_INTAKE_AI'] = 'off'

import importlib                                              # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
EmailClassification, EmailEvent = _main.EmailClassification, _main.EmailEvent
Lead = _main.Lead

from email_ingest import single_message as SM                 # noqa: E402
from email_ingest import webhook as W                         # noqa: E402

with flask_app.app_context():
    db.create_all()


def internal_mail(mid):
    return {'id': 'AAMk' + mid.strip('<>@.'), 'internetMessageId': mid,
            'subject': 'Rates for the Kolkata move',
            'body': {'content': 'Please check internally.'},
            'from': {'emailAddress': {'address': 'someone@procamlogistics.com'}},
            'toRecipients': [{'emailAddress': {'address': 'colleague@procamlogistics.com'}}],
            'ccRecipients': [{'emailAddress': {'address': 'leads@procamgroup.in'}}]}


def _rows(mid):
    with flask_app.app_context():
        return EmailClassification.query.filter_by(message_id=mid).count()


def test_a_decided_message_is_not_classified_again():
    mid = '<once@procamlogistics.com>'
    first = SM.process_single_message(None, 'leads@procamgroup.in',
                                      internal_mail(mid))
    assert first['status'] == 'skipped', first
    assert _rows(mid) == 1

    again = SM.process_single_message(None, 'leads@procamgroup.in',
                                      internal_mail(mid))
    assert again['status'] == 'skipped'
    assert 'already classified' in again['reason'], again
    assert again['classification'] == first['classification']
    assert _rows(mid) == 1


def test_an_administrator_retry_classifies_again():
    mid = '<retry@procamlogistics.com>'
    SM.process_single_message(None, 'leads@procamgroup.in', internal_mail(mid))
    forced = SM.process_single_message(None, 'leads@procamgroup.in',
                                       internal_mail(mid), force=True)
    assert 'already classified' not in (forced.get('reason') or '')
    assert _rows(mid) == 2


def test_the_retry_route_and_restore_script_force_a_fresh_pass():
    src = open(os.path.join(_ROOT, 'app.py')).read()
    assert src.count('msg=msg,\n                                            force=True)') == 2
    restore = open(os.path.join(
        _ROOT, 'scripts', '2026_09_02_restore_wrongly_purged.py')).read()
    assert 'force=True' in restore


def _notification(ref):
    return {'value': [{'clientState': 'hook-secret', 'resource':
                       'users/leads@procamgroup.in/messages/' + ref,
                       'resourceData': {'id': ref}}]}


def test_a_resend_during_processing_is_not_processed(monkeypatch):
    mid = '<busy@customer.com>'
    monkeypatch.setenv('EMAIL_WEBHOOK_SECRET', 'hook-secret')
    monkeypatch.setenv('CRM_INBOX_EMAIL', 'leads@procamgroup.in')
    monkeypatch.delenv('MS_TENANT_ID', raising=False)
    monkeypatch.setattr(W, 'GraphClient', lambda: object())
    monkeypatch.setattr(W, '_mailbox_identifiers',
                        lambda g, m: {'leads@procamgroup.in'})
    monkeypatch.setattr(W, '_get_message',
                        lambda g, m, ref: {'id': ref, 'internetMessageId': mid,
                                           'subject': 'RFQ'})
    calls = []

    def slow_process(graph, mailbox, msg, **kw):
        calls.append(msg['internetMessageId'])
        if len(calls) == 1:
            # Graph's resend arrives while this copy is still working.
            inner = W.handle_notification(_notification('AAMkbusy'))
            assert inner['skipped'] == 1 and inner['processed'] == 0, inner
        return {'status': 'skipped', 'reason': 'C_internal: test'}

    monkeypatch.setattr(W, 'process_single_message', slow_process)
    stats = W.handle_notification(_notification('AAMkbusy'))
    assert stats['processed'] == 1
    assert calls == [mid]
    with flask_app.app_context():
        statuses = sorted(e.status for e in EmailEvent.query.filter_by(
            internet_message_id=mid))
        assert statuses == ['skipped', 'skipped']
        reasons = [e.reason for e in EmailEvent.query.filter_by(
            internet_message_id=mid)]
        assert any('re-sent while event' in (r or '') for r in reasons)


def test_a_crash_while_processing_does_not_leave_the_event_processing(
        monkeypatch):
    mid = '<crash@customer.com>'
    monkeypatch.setenv('EMAIL_WEBHOOK_SECRET', 'hook-secret')
    monkeypatch.setenv('CRM_INBOX_EMAIL', 'leads@procamgroup.in')
    monkeypatch.setattr(W, 'GraphClient', lambda: object())
    monkeypatch.setattr(W, '_mailbox_identifiers',
                        lambda g, m: {'leads@procamgroup.in'})
    monkeypatch.setattr(W, '_get_message',
                        lambda g, m, ref: {'id': ref, 'internetMessageId': mid})

    def boom(*a, **k):
        raise RuntimeError('parser exploded')

    monkeypatch.setattr(W, 'process_single_message', boom)
    try:
        W.handle_notification(_notification('AAMkcrash'))
    except RuntimeError:
        pass
    with flask_app.app_context():
        evt = EmailEvent.query.filter_by(internet_message_id=mid).one()
        assert evt.status == 'failed'


def test_the_poller_does_not_log_a_message_again_each_run():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'poll', os.path.join(_ROOT, 'scripts', '2026_09_02_poll_leads_mailbox.py'))
    poll = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(poll)
    assert poll._seen_before('already ingested')
    assert poll._seen_before('previously purged as irrelevant')
    assert poll._seen_before('C_internal: already classified')
    assert not poll._seen_before('C_internal: sent by a Procam address with '
                                 'the mailbox only copied in')
    assert not poll._seen_before(None)
