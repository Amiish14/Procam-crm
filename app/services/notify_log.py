"""Write down what happened to a notification, and retry what failed.

CRM-USER-ISSUES-02NOV §3.

ONE WRITER, AT THE CHOKEPOINT

`record()` is called from `notify.send` and from nowhere else.
`notify.send` is the one function every notification goes through, so
recording there covers every caller; recording at the call site would
have logged the assignment mail and missed the other callers, which is
the same mistake as filtering archived leads in thirty-three modules
and not in the scope.

NEVER RAISES, NEVER ROLLS BACK THE CALLER

A log that can break the thing it is logging is worse than no log. The
write is attempted in a nested transaction so a failure here — a
missing table on a database that has not run the migration yet, most
likely — cannot take the notification, the bell row or the lead with
it. Everything is wrapped and everything swallows.

WHAT RETRY MEANS HERE, AND WHAT IT DOES NOT

§3 says "retry failures 3 times". Two different failures need two
different answers, and treating them alike would be the easy wrong
thing:

    A transport failure — the mail server refused it, timed out,
    throttled us — is worth retrying, and for anything queued the
    outbox already does it: five attempts over an hour and a half with
    back-off. Nothing is added there. The gap was the
    FEATURE_EMAIL_NOTIFY-off path, which calls the transport directly
    and had no retry at all; `send_with_retries` gives it three.

    A resolution failure — no address on file, not an internal
    address, the person is not an employee any more — is not transient.
    Retrying it three times produces three identical failures and
    hides the one fact that matters, which is that somebody has to fix
    the address. Those are recorded as failed and left for a person,
    and `retryable()` is what tells the two apart.

So: automatic retries for what can succeed on a second try, and a
screen for what cannot. A person retrying from that screen is
deliberate and is allowed either way.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

#: How many times the direct transport is tried before giving up. The
#: outbox path uses its own BACKOFF_MINUTES and is not affected.
DIRECT_ATTEMPTS = 3

#: Errors that will say the same thing every time. Matched on a
#: fragment, lowercased, because the sentences are written for people
#: and will be reworded.
NOT_WORTH_RETRYING = (
    'no email address on file',
    'not an internal address',
    'already queued',
    'muted',
    'no recipient',
)


def retryable(error):
    """Whether trying this again could plausibly work.

    An unknown error is treated as retryable: a transient fault that
    nobody has classified yet should get its three tries, and the cost
    of being wrong is three attempts rather than a notification lost
    for good.
    """
    text = (error or '').strip().lower()
    if not text:
        return True
    return not any(fragment in text for fragment in NOT_WORTH_RETRYING)


def record(*, user_code, status, kind=None, event_key=None, address=None,
           entity_type=None, entity_id=None, title=None, error=None,
           outbox_id=None, attempts=0):
    """One row for one attempt. Returns it, or None if it could not be
    written — which is never allowed to matter."""
    from app import db
    from app.models.notification_log import NotificationAttempt

    try:
        row = NotificationAttempt(
            user_code=(user_code or '')[:20] or None,
            address=(address or '')[:320] or None,
            kind=(kind or '')[:40] or None,
            event_key=(event_key or '')[:60] or None,
            entity_type=(entity_type or '')[:40] or None,
            entity_id=(str(entity_id)[:40] if entity_id is not None
                       else None),
            title=(title or '')[:200] or None,
            status=status,
            error=(error or '')[:300] or None,
            outbox_id=outbox_id,
            attempts=attempts or 0)
        # Nested, so a failure to log cannot roll back the
        # notification that is being logged.
        with db.session.begin_nested():
            db.session.add(row)
        return row
    except Exception:                                     # noqa: BLE001
        log.exception('could not record a notification attempt for %s',
                      user_code)
        return None


def send_with_retries(address, subject, html, *, attempts=None, **extra):
    """The direct transport, tried more than once.

    Returns (ok, error, tried). Only the FEATURE_EMAIL_NOTIFY-off path
    uses this; anything queued is the outbox's business.
    """
    from app.services import mailer

    ceiling = DIRECT_ATTEMPTS if attempts is None else max(1, attempts)
    last = None
    for tried in range(1, ceiling + 1):
        try:
            if mailer.send(address, subject, html, **extra):
                return True, None, tried
            last = 'the mail server refused it'
        except Exception as exc:                          # noqa: BLE001
            last = str(exc)[:200]
            log.warning('direct send to %s failed on attempt %s: %s',
                        address, tried, last)
        if not retryable(last):
            # No point in the remaining tries; the answer will not
            # change and three copies of it bury the reason.
            return False, last, tried
    return False, last, ceiling


# ── the failure screen ───────────────────────────────────────────────
def failures(*, limit=200, entity_type=None, user_code=None,
             include_suppressed=False):
    """Notifications that did not reach anybody, newest first."""
    from app.models.notification_log import (FAILED, SUPPRESSED,
                                             NotificationAttempt)

    wanted = [FAILED] + ([SUPPRESSED] if include_suppressed else [])
    q = NotificationAttempt.query.filter(
        NotificationAttempt.status.in_(wanted))
    if entity_type:
        q = q.filter(NotificationAttempt.entity_type == entity_type)
    if user_code:
        q = q.filter(NotificationAttempt.user_code == user_code)
    return (q.order_by(NotificationAttempt.id.desc())
            .limit(max(1, min(1000, limit))).all())


def for_entity(entity_type, entity_id, *, limit=50):
    """Every notification about one thing — §3's "log every
    notification (lead, …)" read back from the lead's side."""
    from app.models.notification_log import NotificationAttempt

    return (NotificationAttempt.query
            .filter(NotificationAttempt.entity_type == entity_type,
                    NotificationAttempt.entity_id == str(entity_id))
            .order_by(NotificationAttempt.id.desc())
            .limit(limit).all())


def summary(*, days=7):
    """Counts by status over the last few days, for the admin header."""
    from datetime import datetime, timedelta

    from app import db
    from app.models.notification_log import NotificationAttempt

    since = datetime.utcnow() - timedelta(days=max(1, days))
    rows = (db.session.query(NotificationAttempt.status,
                             db.func.count(NotificationAttempt.id))
            .filter(NotificationAttempt.created_at >= since)
            .group_by(NotificationAttempt.status).all())
    return {status: count for status, count in rows}


def retry(row, *, actor):
    """Send one failed notification again, from the screen.

    Deliberate, so it is allowed even for an error `retryable()` would
    not retry on its own: a person who has just corrected an address
    is the case this exists for.
    """
    from app import Employee, db
    from app.models.notification_log import FAILED, QUEUED, SENT
    from app.services import notify

    if row is None:
        return {'ok': False, 'error': 'That notification is not on file.'}
    if row.status != FAILED:
        return {'ok': False,
                'error': f'That one is {row.status}, not failed.'}

    emp = Employee.query.filter_by(emp_code=row.user_code).first()
    if emp is None:
        return {'ok': False,
                'error': f'{row.user_code} is not an employee on file.'}
    address = (getattr(emp, 'email', '') or '').strip()
    if not address:
        return {'ok': False,
                'error': f'{row.user_code} still has no email address. '
                         f'Put one on their record first.'}

    out = notify.send(row.user_code, kind=row.kind or 'retry',
                      title=row.title or 'Notification',
                      event_key=row.event_key,
                      entity_type=row.entity_type,
                      entity_id=row.entity_id,
                      email=True, in_app=False, dedupe=False)
    try:
        from datetime import datetime
        row.retried_by = (actor or '')[:20]
        row.retried_at = datetime.utcnow()
        db.session.commit()
    except Exception:                                     # noqa: BLE001
        db.session.rollback()

    if out.get('queued') or out.get('emailed'):
        return {'ok': True, 'status': QUEUED if out.get('queued') else SENT,
                'address': address}
    return {'ok': False, 'error': out.get('error') or 'It did not go.'}
