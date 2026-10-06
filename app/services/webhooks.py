"""
CRM events the TMS is owed, delivered until they arrive.

The TMS was right to push back: it cannot discover a missed
`lead.won` by polling. A deal that was won and never announced looks
exactly like a deal that was always won, so "poll as well as listen"
was advice that does not work, and saying it did would have been
worse than not having the webhook.

So the event is written down first, inside the caller's transaction,
and a worker delivers it. The business transaction does not depend on
the TMS being up — and the TMS not being up no longer loses the
event.

What is retried, and what is not:

    network error, timeout   retried — the TMS was unreachable
    429                      retried, honouring Retry-After
    5xx                      retried — the TMS was broken
    408, 409, 425            retried — transient by definition
    other 4xx                **dead**. A 400 means the TMS will not
                             accept this payload, and sending it
                             another seven times will not change its
                             mind. Somebody has to look.
    2xx                      delivered

`event_id` is the idempotency key and is identical on every attempt.
A TMS that answers 200 to a replay it has already processed is
telling us it is delivered, which is exactly right — the CRM does not
need to know whether that 200 meant "done" or "done already".
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

LEASE_NAME = 'webhook_outbox'
CLAIM_LIMIT = 25

#: Minutes before each retry. Eight attempts spanning about twelve
#: hours — long enough to ride out a TMS deployment or an overnight
#: outage, short enough that nothing sits unnoticed for a week.
BACKOFF_MINUTES = (1, 5, 15, 60, 120, 360, 720, 720)

#: A row left `sending` this long belongs to a worker that died.
STUCK_MINUTES = 15

#: Retried however many times they take.
RETRY_STATUS = (408, 409, 425, 429)

HTTP_TIMEOUT = 15


def _event_id(event_type, crm_object_id, suffix=None):
    """Stable across retries, unique across events.

    Built from what the event is about rather than from a clock: two
    workers reacting to the same win must produce the same id, and a
    timestamp would give them different ones.
    """
    base = f'{event_type}:{crm_object_id}'
    return f'{base}:{suffix}' if suffix else base


# ── queueing ─────────────────────────────────────────────────────────
def enqueue(event_type, payload, *, crm_object_type=None, crm_object_id=None,
            event_id=None, commit=True, destination=None):
    """Record an event owed to the TMS. Returns the row, or None.

    None means it was already queued — the unique index on `event_id`
    refusing a duplicate, which is the mechanism working rather than a
    failure. Never raises: the caller's business transaction is the
    thing that matters.
    """
    import os

    from app import db
    from app.models.integration import WebhookOutbox

    try:
        key = event_id or _event_id(event_type, crm_object_id)
        existing = WebhookOutbox.query.filter_by(event_id=key).first()
        if existing is not None:
            log.info('webhook %s already queued (status %s)', key,
                     existing.status)
            return None

        row = WebhookOutbox(
            event_id=key[:80],
            event_type=event_type[:60],
            payload_json=json.dumps(payload or {}, default=str),
            destination=(destination
                         or (os.environ.get('TMS_WEBHOOK_URL') or '')
                         )[:500] or None,
            crm_object_type=crm_object_type,
            crm_object_id=(str(crm_object_id)[:60]
                           if crm_object_id is not None else None),
            status='queued',
            next_attempt_at=datetime.utcnow())
        db.session.add(row)
        if commit:
            db.session.commit()
        else:
            db.session.flush()
        return row
    except Exception as exc:                                  # noqa: BLE001
        try:
            db.session.rollback()
        except Exception:
            pass
        log.info('webhook not queued (%s): %s', str(exc)[:120], event_type)
        return None


# ── claiming ─────────────────────────────────────────────────────────
def _unstick(now):
    from app import db
    from app.models.integration import WebhookOutbox
    from sqlalchemy import update

    cutoff = now - timedelta(minutes=STUCK_MINUTES)
    result = db.session.execute(
        update(WebhookOutbox.__table__)
        .where(WebhookOutbox.__table__.c.status == 'sending')
        .where(WebhookOutbox.__table__.c.claimed_at < cutoff)
        .values(status='queued', claimed_by=None, claimed_at=None))
    db.session.commit()
    return result.rowcount or 0


def claim(limit=CLAIM_LIMIT, now=None, worker=None):
    """Take up to `limit` due events. Same shape as the email outbox:
    SQLite has no SKIP LOCKED, so a conditional UPDATE does it."""
    from app import db
    from app.models.integration import WebhookOutbox
    from app.services import leases
    from sqlalchemy import update

    now = now or datetime.utcnow()
    worker = worker or leases.me()

    due = (WebhookOutbox.query
           .filter(WebhookOutbox.status.in_(('queued', 'failed')),
                   WebhookOutbox.next_attempt_at <= now)
           .order_by(WebhookOutbox.next_attempt_at, WebhookOutbox.id)
           .limit(limit).all())
    ids = [r.id for r in due]
    if not ids:
        return []

    db.session.execute(
        update(WebhookOutbox.__table__)
        .where(WebhookOutbox.__table__.c.id.in_(ids))
        .where(WebhookOutbox.__table__.c.status.in_(('queued', 'failed')))
        .values(status='sending', claimed_by=worker, claimed_at=now))
    db.session.commit()

    return (WebhookOutbox.query
            .filter(WebhookOutbox.id.in_(ids),
                    WebhookOutbox.status == 'sending',
                    WebhookOutbox.claimed_by == worker)
            .order_by(WebhookOutbox.id).all())


# ── delivering ───────────────────────────────────────────────────────
def body_for(row):
    """What goes on the wire. The same shape every time, so a retry is
    byte-identical apart from its request id."""
    try:
        data = json.loads(row.payload_json or '{}')
    except Exception:
        data = {}
    return {
        'event': row.event_type,
        'event_id': row.event_id,
        'request_id': row.request_id or '',
        'sent_at': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
        'source': 'procam-crm',
        'attempt': (row.attempts or 0) + 1,
        'data': data,
    }


def deliver(row, *, post=None, now=None):
    """One attempt. Returns 'delivered' | 'failed' | 'dead'."""
    import os

    from app import db
    from app.services import integration as integ

    now = now or datetime.utcnow()
    row.request_id = uuid.uuid4().hex
    row.attempts = (row.attempts or 0) + 1

    url = (row.destination or os.environ.get('TMS_WEBHOOK_URL') or '').strip()
    token = os.environ.get('TMS_WEBHOOK_TOKEN') or ''
    if not url:
        return _dead(row, now, 0, 'TMS_WEBHOOK_URL is not set')

    headers = {
        'Content-Type': 'application/json',
        'X-Request-Id': row.request_id,
        # The same value on every attempt. This is what the TMS
        # deduplicates on.
        'Idempotency-Key': row.event_id,
        'X-Event-Id': row.event_id,
    }
    if token:
        headers['Authorization'] = f'Bearer {token}'

    body = body_for(row)
    started = time.monotonic()
    try:
        poster = post or _post
        status_code, text, retry_after = poster(url, body, headers,
                                                HTTP_TIMEOUT)
    except Exception as exc:                                  # noqa: BLE001
        outcome = _retry(row, now, 0, f'{type(exc).__name__}: '
                                      f'{str(exc)[:200]}')
        _log(integ, row, url, 0, None, str(exc)[:400], started, body)
        return outcome

    # 2xx, including a replay the TMS has already processed. The CRM
    # does not need to know whether 200 meant "done" or "done
    # already" — both mean it has it.
    if 200 <= status_code < 300:
        row.status = 'delivered'
        row.delivered_at = now
        row.last_status_code = status_code
        row.last_error = None
        row.claimed_by = None
        db.session.commit()
        _log(integ, row, url, status_code, text, None, started, body)
        return 'delivered'

    if status_code in RETRY_STATUS or status_code >= 500:
        wait = retry_after if (status_code == 429 and retry_after) else None
        outcome = _retry(row, now, status_code,
                         f'HTTP {status_code}', override_minutes=wait)
        _log(integ, row, url, status_code, text, f'HTTP {status_code}',
             started, body)
        return outcome

    # Any other 4xx. The TMS will not accept this payload, and sending
    # it seven more times will not change that.
    outcome = _dead(row, now, status_code,
                    f'HTTP {status_code} — not retryable')
    _log(integ, row, url, status_code, text,
         f'HTTP {status_code} (permanent)', started, body)
    return outcome


def _post(url, body, headers, timeout):
    """(status_code, text, retry_after_minutes)."""
    import requests

    resp = requests.post(url, json=body, headers=headers, timeout=timeout)
    retry_after = None
    raw = resp.headers.get('Retry-After')
    if raw:
        try:
            retry_after = max(1, int(float(raw)) // 60) or 1
        except (TypeError, ValueError):
            retry_after = None
    return resp.status_code, (resp.text or '')[:2000], retry_after


def _retry(row, now, status_code, error, override_minutes=None):
    from app import db

    row.last_status_code = status_code or None
    row.last_error = (error or '')[:500]
    row.claimed_by = None
    row.claimed_at = None
    if (row.attempts or 0) >= (row.max_attempts or len(BACKOFF_MINUTES)):
        row.status = 'dead'
        row.last_error = (f'{error} — gave up after {row.attempts} '
                          f'attempts')[:500]
        db.session.commit()
        return 'dead'
    index = min((row.attempts or 1) - 1, len(BACKOFF_MINUTES) - 1)
    minutes = override_minutes or BACKOFF_MINUTES[index]
    row.status = 'failed'
    row.next_attempt_at = now + timedelta(minutes=minutes)
    db.session.commit()
    return 'failed'


def _dead(row, now, status_code, error):
    from app import db

    row.status = 'dead'
    row.last_status_code = status_code or None
    row.last_error = (error or '')[:500]
    row.claimed_by = None
    db.session.commit()
    return 'dead'


def _log(integ, row, url, status_code, response, error, started, body):
    """Into the same integration log as everything else.

    `_trim` already removes anything whose key looks like a secret, so
    the Authorization header is not written here — and it is not
    passed in either.
    """
    try:
        integ.record(
            direction='outbound', endpoint=f'{url}#{row.event_type}',
            method='POST',
            status='ok' if row.status == 'delivered' else
                   ('error' if row.status != 'dead' else 'refused'),
            status_code=status_code, request_id=row.request_id,
            idempotency_key=row.event_id,
            crm_object_type=row.crm_object_type,
            crm_object_id=row.crm_object_id,
            request_summary=body, response_summary=response, error=error,
            caller='procam-crm', started=started)
    except Exception:
        log.exception('could not log the webhook attempt')


# ── the worker ───────────────────────────────────────────────────────
def run_once(limit=CLAIM_LIMIT, now=None, post=None, use_lease=True):
    """One pass. Returns a small report."""
    from app.services import leases

    now = now or datetime.utcnow()
    report = {'claimed': 0, 'delivered': 0, 'failed': 0, 'dead': 0,
              'unstuck': 0, 'skipped': 'no'}

    def _work():
        report['unstuck'] = _unstick(now)
        rows = claim(limit=limit, now=now)
        report['claimed'] = len(rows)
        for row in rows:
            report[deliver(row, post=post, now=now)] += 1

    if not use_lease:
        _work()
        return report
    with leases.hold(LEASE_NAME, seconds=300) as got:
        if not got:
            report['skipped'] = 'another worker holds the lease'
            return report
        _work()
    return report


def health(now=None):
    from app import db
    from app.models.integration import WebhookOutbox
    from sqlalchemy import func

    now = now or datetime.utcnow()
    counts = dict(db.session.query(WebhookOutbox.status,
                                   func.count(WebhookOutbox.id))
                  .group_by(WebhookOutbox.status).all())
    oldest = (WebhookOutbox.query
              .filter(WebhookOutbox.status.in_(('queued', 'failed')))
              .order_by(WebhookOutbox.next_attempt_at).first())
    dead = (WebhookOutbox.query.filter(WebhookOutbox.status == 'dead')
            .order_by(WebhookOutbox.id.desc()).limit(20).all())
    return {
        'counts': counts,
        'waiting': counts.get('queued', 0) + counts.get('failed', 0),
        'dead': counts.get('dead', 0),
        'oldest_waiting_minutes': (
            int((now - oldest.next_attempt_at).total_seconds() // 60)
            if oldest and oldest.next_attempt_at
            and oldest.next_attempt_at < now else 0),
        'dead_rows': [d.to_dict() for d in dead],
    }


def retry(ids, now=None):
    """Put dead events back. For after somebody has fixed the cause."""
    from app import db
    from app.models.integration import WebhookOutbox
    from sqlalchemy import update

    now = now or datetime.utcnow()
    result = db.session.execute(
        update(WebhookOutbox.__table__)
        .where(WebhookOutbox.__table__.c.id.in_(list(ids)))
        .where(WebhookOutbox.__table__.c.status == 'dead')
        .values(status='queued', attempts=0, next_attempt_at=now,
                last_error=None, claimed_by=None, claimed_at=None))
    db.session.commit()
    return result.rowcount or 0
