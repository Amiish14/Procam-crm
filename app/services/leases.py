"""
One runner at a time.

The CRM is two gunicorn workers and a handful of systemd timers sharing
one SQLite file. Nothing here may run twice at once: two outbox workers
would each claim the same queued row between the other's read and
write, and the person would get the email twice.

On PostgreSQL this is `pg_try_advisory_lock`. On SQLite the equivalent
is a row with an expiry, taken by a conditional UPDATE: SQLite
serialises writers, so of two processes issuing

    UPDATE job_leases SET holder = :me, expires_at = :then
     WHERE name = :job AND (expires_at IS NULL OR expires_at < :now)

exactly one sees rowcount 1. The expiry is what makes it safe against a
process that is killed mid-run: the lease lapses and the next run takes
it, rather than the job stopping forever because a lock was never let
go.

    with leases.hold('outbox', seconds=300) as got:
        if not got:
            return
        ...

Keep the lease shorter than the gap between runs of the job, and longer
than the job takes. `renew()` exists for a long run.
"""
from __future__ import annotations

import contextlib
import os
import socket
from datetime import datetime, timedelta

#: Default lease length. The outbox worker runs every two minutes and
#: finishes in seconds; five minutes is room for a slow Graph call
#: without holding the job back after a crash.
DEFAULT_SECONDS = 300


def me():
    """Who is asking — host and pid, enough to find the process."""
    try:
        host = socket.gethostname()
    except Exception:                                       # pragma: no cover
        host = 'unknown'
    return f'{host}:{os.getpid()}'[:120]


def acquire(name, seconds=DEFAULT_SECONDS, holder=None, now=None):
    """Take the lease. True if we hold it, False if somebody else does."""
    from app import db
    from app.models.mailops import JobLease
    from sqlalchemy import update

    now = now or datetime.utcnow()
    until = now + timedelta(seconds=seconds)
    holder = holder or me()

    row = JobLease.query.filter_by(name=name).first()
    if row is None:
        try:
            db.session.add(JobLease(name=name, holder=holder,
                                    acquired_at=now, expires_at=until))
            db.session.commit()
            return True
        except Exception:
            # Another process inserted it first — the unique index did
            # its job. Fall through and try to take it the normal way.
            db.session.rollback()

    res = db.session.execute(
        update(JobLease.__table__)
        .where(JobLease.__table__.c.name == name)
        .where((JobLease.__table__.c.expires_at.is_(None))
               | (JobLease.__table__.c.expires_at < now))
        .values(holder=holder, acquired_at=now, expires_at=until))
    db.session.commit()
    return bool(res.rowcount)


def renew(name, seconds=DEFAULT_SECONDS, holder=None, now=None):
    """Extend a lease we hold. False if we have lost it."""
    from app import db
    from app.models.mailops import JobLease
    from sqlalchemy import update

    now = now or datetime.utcnow()
    holder = holder or me()
    res = db.session.execute(
        update(JobLease.__table__)
        .where(JobLease.__table__.c.name == name)
        .where(JobLease.__table__.c.holder == holder)
        .values(expires_at=now + timedelta(seconds=seconds)))
    db.session.commit()
    return bool(res.rowcount)


def release(name, holder=None):
    """Give it back early so the next run does not wait out the expiry."""
    from app import db
    from app.models.mailops import JobLease
    from sqlalchemy import update

    holder = holder or me()
    try:
        db.session.execute(
            update(JobLease.__table__)
            .where(JobLease.__table__.c.name == name)
            .where(JobLease.__table__.c.holder == holder)
            .values(expires_at=datetime.utcnow() - timedelta(seconds=1)))
        db.session.commit()
    except Exception:
        db.session.rollback()


@contextlib.contextmanager
def hold(name, seconds=DEFAULT_SECONDS):
    """Context manager yielding whether we got it. Always releases."""
    holder = me()
    got = False
    try:
        got = acquire(name, seconds=seconds, holder=holder)
        yield got
    finally:
        if got:
            release(name, holder=holder)


def status():
    """Every lease, for the health screen."""
    from app.models.mailops import JobLease
    now = datetime.utcnow()
    out = []
    for row in JobLease.query.order_by(JobLease.name).all():
        out.append({
            'name': row.name,
            'holder': row.holder or '',
            'held': bool(row.expires_at and row.expires_at > now),
            'expires_at': str(row.expires_at)[:19] if row.expires_at else '',
        })
    return out
