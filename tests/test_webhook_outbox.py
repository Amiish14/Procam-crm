"""Events the TMS is owed, delivered until they arrive.

The TMS cannot discover a `lead.won` it never received — a deal that
was won and never announced looks identical, when polled, to a deal
that was always won. So "poll as well as listen" was advice that does
not work, and these tests cover the thing that replaced it.

Nothing here touches a real TMS. Every delivery goes through an
injected poster.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'webhook-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'WebhookTest12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'webhooks.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db

from app.models.integration import (                      # noqa: E402
    IntegrationLog, WebhookOutbox)
from app.services import webhooks                         # noqa: E402

TOKEN = 'a-webhook-token-that-is-not-real'


class Poster:
    """Stands in for the TMS. Records what it was sent."""

    def __init__(self, *responses):
        self.responses = list(responses) or [(200, 'ok', None)]
        self.calls = []

    def __call__(self, url, body, headers, timeout):
        self.calls.append({'url': url, 'body': body, 'headers': headers})
        if len(self.responses) > 1:
            return self.responses.pop(0)
        response = self.responses[0]
        if isinstance(response, Exception):
            raise response
        return response


def _boom(exc):
    def _raise(url, body, headers, timeout):
        raise exc
    return _raise


@pytest.fixture()
def world(monkeypatch):
    monkeypatch.setenv('TMS_WEBHOOK_URL', 'https://tms.invalid/crm/webhook')
    monkeypatch.setenv('TMS_WEBHOOK_TOKEN', TOKEN)
    with flask_app.app_context():
        db.create_all()
        WebhookOutbox.query.delete(synchronize_session=False)
        IntegrationLog.query.delete(synchronize_session=False)
        db.session.commit()
        yield
        WebhookOutbox.query.delete(synchronize_session=False)
        IntegrationLog.query.delete(synchronize_session=False)
        db.session.commit()


def _queue(event='lead.won', object_id=1):
    return webhooks.enqueue(event, {'crm_lead_id': object_id},
                            crm_object_type='Lead', crm_object_id=object_id)


# ── queued and delivered ─────────────────────────────────────────────
@pytest.mark.parametrize('event', ['lead.won', 'quote.won'])
def test_an_event_is_queued_then_delivered(world, event):
    _queue(event)
    poster = Poster((200, '{"ok":true}', None))
    report = webhooks.run_once(post=poster, use_lease=False)
    assert report['delivered'] == 1
    row = WebhookOutbox.query.one()
    assert row.status == 'delivered' and row.delivered_at is not None
    assert row.attempts == 1
    assert poster.calls[0]['body']['event'] == event


def test_the_business_transaction_does_not_wait_for_the_tms(world):
    """Queueing is a row, not a network call."""
    row = _queue()
    assert row.status == 'queued'
    assert WebhookOutbox.query.one().attempts == 0


def test_a_delivered_event_is_not_sent_again(world):
    _queue()
    webhooks.run_once(post=Poster((200, 'ok', None)), use_lease=False)
    second = Poster((200, 'ok', None))
    report = webhooks.run_once(post=second, use_lease=False)
    assert report['claimed'] == 0 and second.calls == []


# ── what is retried ──────────────────────────────────────────────────
@pytest.mark.parametrize('code', [500, 502, 503, 429, 408, 409, 425])
def test_a_transient_failure_is_retried(world, code):
    _queue()
    webhooks.run_once(post=Poster((code, 'later', None)), use_lease=False)
    row = WebhookOutbox.query.one()
    assert row.status == 'failed', (code, row.status)
    assert row.attempts == 1
    assert row.next_attempt_at > datetime.utcnow()
    assert row.last_status_code == code


def test_the_tms_being_unreachable_is_retried(world):
    _queue()
    report = webhooks.run_once(post=_boom(OSError('connection refused')),
                               use_lease=False)
    assert report['failed'] == 1
    assert WebhookOutbox.query.one().status == 'failed'


def test_a_timeout_is_retried(world):
    _queue()
    webhooks.run_once(post=_boom(TimeoutError('read timed out')),
                      use_lease=False)
    row = WebhookOutbox.query.one()
    assert row.status == 'failed'
    assert 'TimeoutError' in (row.last_error or '')


def test_a_429_honours_retry_after(world):
    """Delivered directly rather than through run_once: a fixed `now`
    in the past makes claim() skip the row, and the assertion then
    compares a timestamp nothing touched."""
    row = _queue()
    now = datetime(2026, 10, 6, 12, 0)
    outcome = webhooks.deliver(row, post=Poster((429, 'slow down', 30)),
                               now=now)
    assert outcome == 'failed'
    assert row.next_attempt_at == now + timedelta(minutes=30), (
        'Retry-After was ignored')


def test_without_retry_after_a_429_uses_the_ordinary_backoff(world):
    row = _queue()
    now = datetime(2026, 10, 6, 12, 0)
    webhooks.deliver(row, post=Poster((429, 'slow down', None)), now=now)
    assert row.next_attempt_at == now + timedelta(
        minutes=webhooks.BACKOFF_MINUTES[0])


# ── what is not retried ──────────────────────────────────────────────
@pytest.mark.parametrize('code', [400, 401, 403, 404, 422])
def test_a_permanent_rejection_is_not_retried_for_ever(world, code):
    """A 400 means the TMS will not accept this payload. Sending it
    another seven times will not change its mind."""
    _queue()
    webhooks.run_once(post=Poster((code, 'no', None)), use_lease=False)
    row = WebhookOutbox.query.one()
    assert row.status == 'dead', (code, row.status)
    assert row.attempts == 1

    after = Poster((200, 'ok', None))
    assert webhooks.run_once(post=after, use_lease=False)['claimed'] == 0
    assert after.calls == []


def test_it_gives_up_after_the_attempt_ceiling(world):
    row = _queue()
    row.attempts = (row.max_attempts or 8) - 1
    db.session.commit()
    webhooks.run_once(post=Poster((503, 'down', None)), use_lease=False)
    assert WebhookOutbox.query.one().status == 'dead'


def test_a_dead_event_can_be_revived_once_somebody_fixes_the_cause(world):
    _queue()
    webhooks.run_once(post=Poster((400, 'bad', None)), use_lease=False)
    row = WebhookOutbox.query.one()
    assert webhooks.retry([row.id]) == 1
    db.session.expire_all()
    assert WebhookOutbox.query.one().status == 'queued'
    report = webhooks.run_once(post=Poster((200, 'ok', None)),
                               use_lease=False)
    assert report['delivered'] == 1


# ── idempotency ──────────────────────────────────────────────────────
def test_every_retry_carries_the_same_event_id(world):
    _queue()
    poster = Poster((503, 'down', None), (503, 'down', None),
                    (200, 'ok', None))
    seen = []
    for _ in range(3):
        row = WebhookOutbox.query.one()
        row.next_attempt_at = datetime.utcnow() - timedelta(minutes=1)
        row.status = 'queued' if row.status == 'failed' else row.status
        db.session.commit()
        webhooks.run_once(post=poster, use_lease=False)
        seen.append(poster.calls[-1]['headers']['Idempotency-Key'])

    assert len(set(seen)) == 1, 'the idempotency key changed between tries'
    assert seen[0] == 'lead.won:1'
    request_ids = [c['headers']['X-Request-Id'] for c in poster.calls]
    assert len(set(request_ids)) == 3, (
        'each attempt needs its own request id for the logs')


def test_the_same_business_event_cannot_be_queued_twice(world):
    assert _queue('lead.won', 5) is not None
    assert _queue('lead.won', 5) is None
    assert WebhookOutbox.query.count() == 1


def test_a_duplicate_the_tms_has_already_processed_counts_as_delivered(world):
    """If the TMS answers 2xx to a replay, it has the event. The CRM
    does not need to know whether that meant "done" or "done
    already"."""
    _queue()
    webhooks.run_once(post=Poster((200, '{"duplicate":true}', None)),
                      use_lease=False)
    assert WebhookOutbox.query.one().status == 'delivered'


# ── the secret ───────────────────────────────────────────────────────
def test_the_token_is_sent_but_never_written_down(world):
    _queue()
    poster = Poster((500, 'broken', None))
    webhooks.run_once(post=poster, use_lease=False)

    assert poster.calls[0]['headers']['Authorization'] == f'Bearer {TOKEN}'

    row = WebhookOutbox.query.one()
    written = ' '.join(str(v) for v in (
        row.last_error, row.payload_json, row.destination))
    logged = ' '.join(
        (l.request_summary or '') + (l.response_summary or '')
        + (l.error or '')
        for l in IntegrationLog.query.all())
    assert TOKEN not in written
    assert TOKEN not in logged
    assert 'Authorization' not in logged


# ── surviving a restart ──────────────────────────────────────────────
def test_a_worker_dying_mid_flight_does_not_lose_the_event(world):
    """The row is claimed and the process is killed. Nothing marks it
    delivered or failed, so it must come back rather than sit in
    `sending` for ever."""
    _queue()
    claimed = webhooks.claim(worker='worker-that-died')
    assert len(claimed) == 1 and claimed[0].status == 'sending'

    claimed[0].claimed_at = datetime.utcnow() - timedelta(
        minutes=webhooks.STUCK_MINUTES + 1)
    db.session.commit()

    report = webhooks.run_once(post=Poster((200, 'ok', None)),
                               use_lease=False)
    assert report['unstuck'] == 1
    assert report['delivered'] == 1


def test_a_queued_event_survives_being_read_back_from_the_database(world):
    """Durability is the whole point: the queue is a table, not a
    thread's memory."""
    _queue('quote.won', 42)
    db.session.expire_all()
    row = WebhookOutbox.query.filter_by(event_id='quote.won:42').one()
    assert json.loads(row.payload_json)['crm_lead_id'] == 42
    assert row.status == 'queued'


def test_two_workers_cannot_claim_the_same_event(world):
    _queue()
    first = webhooks.claim(worker='worker-a')
    second = webhooks.claim(worker='worker-b')
    assert len(first) == 1 and second == []


def test_a_held_lease_stops_a_second_worker(world):
    from app.services import leases

    _queue()
    assert leases.acquire(webhooks.LEASE_NAME, holder='someone-else')
    try:
        report = webhooks.run_once(post=Poster((200, 'ok', None)))
        assert report['skipped'] != 'no' and report['claimed'] == 0
    finally:
        leases.release(webhooks.LEASE_NAME, holder='someone-else')


# ── health ───────────────────────────────────────────────────────────
def test_health_separates_waiting_from_dead(world):
    _queue('lead.won', 1)
    _queue('lead.won', 2)
    webhooks.run_once(post=Poster((400, 'bad', None)), use_lease=False)
    report = webhooks.health()
    assert report['dead'] >= 1
    assert report['dead_rows'][0]['last_error']
