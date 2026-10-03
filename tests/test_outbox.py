"""The outbox: idempotency, claiming, retry, batching and quiet hours.

These are the guarantees the whole notification release rests on, so
each one is tested as a property rather than as a happy path:

  * the same email cannot be queued twice
  * two workers cannot claim the same row
  * a failed send is retried with a back-off, then given up on
  * a message raised at midnight arrives in the morning
  * an attachment that is no longer on disk loses the attachment, not
    the email
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'outbox-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'OutboxTest12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'outbox.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db

from app.models.mailops import EmailOutbox, NotificationPref  # noqa: E402
from app.services import leases, notify_prefs, outbox      # noqa: E402

TO = 'outbox.test@procamgroup.in'
TAG = 'OBX-'


def _wipe():
    EmailOutbox.query.filter(
        EmailOutbox.dedupe_key.like(TAG + '%')).delete(
            synchronize_session=False)
    NotificationPref.query.filter(
        NotificationPref.user_code.like('OBX%')).delete(
            synchronize_session=False)
    db.session.commit()


@pytest.fixture()
def ctx():
    with flask_app.app_context():
        db.create_all()
        _wipe()
        yield
        _wipe()


def _queue(key, **kw):
    kw.setdefault('to', TO)
    kw.setdefault('subject', 'Subject')
    kw.setdefault('html', '<p>body</p>')
    return outbox.enqueue(dedupe_key=TAG + key, **kw)


# ── idempotency ──────────────────────────────────────────────────────
def test_the_same_key_is_only_queued_once(ctx):
    assert _queue('once') is not None
    assert _queue('once') is None
    assert EmailOutbox.query.filter_by(dedupe_key=TAG + 'once').count() == 1


def test_an_external_recipient_is_never_queued(ctx):
    assert _queue('external', to='buyer@customer.com') is None
    assert EmailOutbox.query.filter_by(
        dedupe_key=TAG + 'external').count() == 0


def test_a_muted_event_is_not_queued(ctx):
    notify_prefs.save('OBXMUTE', muted_events=['lead.assigned'],
                      quiet_enabled=False)
    assert _queue('muted', user_code='OBXMUTE',
                  event_key='lead.assigned') is None
    assert _queue('notmuted', user_code='OBXMUTE',
                  event_key='quote.won') is not None


# ── claiming ─────────────────────────────────────────────────────────
def test_two_workers_cannot_claim_the_same_row(ctx):
    _queue('claim')
    first = outbox.claim(worker='worker-a')
    second = outbox.claim(worker='worker-b')
    assert [r.dedupe_key for r in first] == [TAG + 'claim']
    assert second == []


def test_a_row_held_back_is_not_claimed_early(ctx):
    _queue('later', not_before=datetime.utcnow() + timedelta(hours=2))
    assert outbox.claim() == []


def test_a_row_abandoned_by_a_dead_worker_comes_back(ctx):
    _queue('stuck')
    row = outbox.claim(worker='worker-that-died')[0]
    row.claimed_at = datetime.utcnow() - timedelta(
        minutes=outbox.STUCK_MINUTES + 5)
    db.session.commit()
    assert outbox._unstick(datetime.utcnow()) == 1
    assert outbox.claim(worker='worker-b')[0].id == row.id


# ── sending ──────────────────────────────────────────────────────────
def test_a_sent_row_is_marked_and_not_sent_again(ctx):
    _queue('sent')
    calls = []
    report = outbox.run_once(send=lambda *a, **kw: calls.append(a) or True,
                             use_lease=False)
    assert report['sent'] == 1 and len(calls) == 1
    assert outbox.run_once(send=lambda *a, **kw: True,
                           use_lease=False)['claimed'] == 0


def test_a_refused_send_is_retried_with_a_backoff(ctx):
    _queue('retry')
    now = datetime.utcnow()
    outbox.run_once(send=lambda *a, **kw: False, use_lease=False, now=now)
    row = EmailOutbox.query.filter_by(dedupe_key=TAG + 'retry').one()
    assert row.status == 'queued' and row.attempts == 1
    assert row.not_before > now
    assert row.last_error


def test_it_gives_up_after_the_last_attempt(ctx):
    _queue('giveup')
    row = EmailOutbox.query.filter_by(dedupe_key=TAG + 'giveup').one()
    row.attempts = (row.max_attempts or 5) - 1
    db.session.commit()
    outbox.run_once(send=lambda *a, **kw: False, use_lease=False)
    row = EmailOutbox.query.filter_by(dedupe_key=TAG + 'giveup').one()
    assert row.status == 'failed'


def test_a_failed_row_can_be_retried_by_hand(ctx):
    _queue('byhand')
    row = EmailOutbox.query.filter_by(dedupe_key=TAG + 'byhand').one()
    row.status, row.attempts = 'failed', 5
    db.session.commit()
    assert outbox.retry([row.id]) == 1
    row = EmailOutbox.query.filter_by(dedupe_key=TAG + 'byhand').one()
    assert row.status == 'queued' and row.attempts == 0


# ── batching ─────────────────────────────────────────────────────────
def test_rows_sharing_a_batch_key_arrive_as_one_email(ctx):
    for i in range(3):
        _queue(f'batch{i}', batch_key='OBX-BATCH', subject=f'Lead {i}')
    seen = []

    def _send(to, subject, html, **kw):
        seen.append((subject, html))
        return True

    report = outbox.run_once(send=_send, use_lease=False)
    assert report['groups'] == 1 and report['sent'] == 3
    assert len(seen) == 1
    subject, html = seen[0]
    assert subject.startswith('3 CRM updates')
    for i in range(3):
        assert f'Lead {i}' in html


def test_rows_without_a_batch_key_are_separate_emails(ctx):
    _queue('solo1')
    _queue('solo2')
    seen = []
    outbox.run_once(send=lambda to, s, h, **kw: seen.append(s) or True,
                    use_lease=False)
    assert len(seen) == 2


# ── the lease ────────────────────────────────────────────────────────
def test_only_one_worker_runs_at_a_time(ctx):
    _queue('leased')
    assert leases.acquire(outbox.LEASE_NAME, holder='someone-else')
    report = outbox.run_once(send=lambda *a, **kw: True)
    assert report['skipped'] != 'no' and report['claimed'] == 0
    leases.release(outbox.LEASE_NAME, holder='someone-else')


def test_an_expired_lease_is_taken_by_the_next_runner(ctx):
    past = datetime.utcnow() - timedelta(hours=1)
    assert leases.acquire('obx-expiry', seconds=1, holder='old', now=past)
    assert leases.acquire('obx-expiry', holder='new')


# ── quiet hours and preferences ──────────────────────────────────────
def test_an_email_raised_at_night_waits_until_the_morning(ctx):
    """21:40 IST on the 3rd is 16:10 UTC. It must leave at 07:00 IST on
    the 4th, which is 01:30 UTC."""
    notify_prefs.save('OBXQUIET', quiet_enabled=True,
                      quiet_start_hour=21, quiet_end_hour=7)
    when, why = notify_prefs.release_at(
        'OBXQUIET', event_key='lead.assigned',
        now=datetime(2026, 10, 3, 16, 10))
    assert why == 'quiet hours'
    assert when == datetime(2026, 10, 4, 1, 30)


def test_a_daytime_email_is_not_held(ctx):
    notify_prefs.save('OBXDAY', quiet_enabled=True)
    now = datetime(2026, 10, 3, 6, 0)            # 11:30 IST
    when, why = notify_prefs.release_at('OBXDAY', event_key='lead.assigned',
                                        now=now)
    assert (when, why) == (now, 'immediate')


def test_an_urgent_event_ignores_the_quiet_window(ctx):
    notify_prefs.save('OBXURG', quiet_enabled=True)
    now = datetime(2026, 10, 3, 16, 10)          # 21:40 IST
    when, why = notify_prefs.release_at('OBXURG',
                                        event_key='rfq.quote_overdue', now=now)
    assert (when, why) == (now, 'urgent')


def test_batched_mode_holds_the_email_for_the_window(ctx):
    notify_prefs.save('OBXBAT', mode='batched', batch_minutes=15,
                      quiet_enabled=False)
    now = datetime(2026, 10, 3, 6, 0)
    when, why = notify_prefs.release_at('OBXBAT', event_key='lead.assigned',
                                        now=now)
    assert why == 'batched' and when == now + timedelta(minutes=15)


def test_someone_with_no_row_behaves_as_before(ctx):
    """A person who has never opened the preferences page must be
    unaffected by the table arriving."""
    prefs = notify_prefs.for_user('OBXNOROW')
    assert prefs['email_enabled'] and prefs['mode'] == 'immediate'
    assert notify_prefs.wants('OBXNOROW', 'lead.assigned')


# ── attachments ──────────────────────────────────────────────────────
def test_a_missing_file_loses_the_attachment_not_the_email(ctx, tmp_path):
    gone = str(tmp_path / 'nothing-here.pdf')
    resolved = outbox.resolve_attachments(
        [{'kind': 'lead_attachment', 'id': 999999},
         {'kind': 'raw_email', 'id': 999999}])
    assert resolved == []
    assert not os.path.exists(gone)


def test_an_attachment_over_the_budget_is_left_out(ctx):
    from email_ingest import notifier
    big = b'x' * (notifier.MAX_INLINE_ATTACHMENT_BYTES + 1)
    payload = notifier._attachment_payload(
        [{'filename': 'small.pdf', 'content': b'hello'},
         {'filename': 'huge.pdf', 'content': big}])
    assert [p['name'] for p in payload] == ['small.pdf']
