"""
The transactional outbox: write the email down, send it later.

Before this, an email was sent inside the request that caused it. That
has three faults. A slow mail server makes the CRM slow. A send that
fails is lost, because nothing recorded that it should have happened.
And a send that succeeds inside a transaction that then rolls back has
told somebody about a change that did not happen.

Queueing fixes all three: `enqueue()` writes one row in the caller's
transaction — if the caller rolls back, the email was never queued —
and a worker owns delivery, retry and the record of what happened.

Idempotency is `dedupe_key`, unique in the database. The webhook that
fires twice, the button clicked twice, the sweep that overlaps itself:
all three write the same key, and the second write is refused by the
database rather than by a query that two writers could race through.

Claiming is the SQLite equivalent of `FOR UPDATE SKIP LOCKED`:

    UPDATE email_outbox SET status='sending', claimed_by=:me
     WHERE id IN (...) AND status='queued'

SQLite serialises writers, so of two workers issuing that statement for
the same row exactly one gets rowcount 1 — and `leases.hold('outbox')`
means there should only ever be one worker anyway. Both, because the
belt costs nothing and the braces are what you want at 03:00.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

#: Rows claimed per worker pass. Large enough to clear a morning's
#: queue in a couple of passes, small enough that one bad batch does
#: not hold the lease for minutes.
CLAIM_LIMIT = 50

#: Retry back-off in minutes, by attempt number. After the last one the
#: row is marked failed and waits for a person.
BACKOFF_MINUTES = (2, 10, 30, 120, 360)

#: A row stuck in `sending` for longer than this was claimed by a
#: worker that died. Returned to the queue by the next pass.
STUCK_MINUTES = 30

LEASE_NAME = 'email_outbox'


# ── queueing ─────────────────────────────────────────────────────────
def make_key(*parts):
    """A stable dedupe key. Long keys are hashed so the column holds."""
    raw = '|'.join(str(p) for p in parts if p is not None)
    if len(raw) <= 190:
        return raw
    return raw[:150] + ':' + hashlib.sha256(raw.encode()).hexdigest()[:32]


def enqueue(*, to, subject, html, dedupe_key, event_key='', user_code=None,
            cc=None, text=None, attachments=None, entity_type=None,
            entity_id=None, not_before=None, batch_key=None, commit=True,
            urgent=None):
    """Queue one email. Returns the row, or None if it was not queued.

    Not queued means one of: no internal recipient, the person has
    muted this event, or the same key is already in the outbox. All
    three are ordinary outcomes, not failures, and none of them raise —
    the caller's work has already happened.
    """
    from app import db
    from app.models.mailops import EmailOutbox
    from app.services import mail_policy, notify_prefs

    try:
        recipients = [to] if isinstance(to, str) else list(to or [])
        allowed, refused = mail_policy.check(recipients)
        if refused:
            log.error('outbox refused external recipient(s) %s for %r',
                      ', '.join(refused), subject[:60])
        if not allowed:
            return None

        if user_code and event_key and not notify_prefs.wants(user_code,
                                                              event_key):
            return None

        if not_before is None:
            when, _why = notify_prefs.release_at(
                user_code, event_key=event_key, urgent=urgent)
            not_before = when

        row = EmailOutbox(
            dedupe_key=make_key(dedupe_key),
            event_key=(event_key or '')[:60],
            user_code=(user_code or None),
            to_addr=', '.join(allowed)[:1000],
            cc_addr=(', '.join(mail_policy.check(list(cc or []))[0])[:1000]
                     or None),
            subject=(subject or '(no subject)')[:400],
            html=html or '',
            text_body=text,
            attachments_json=(json.dumps(attachments) if attachments else None),
            entity_type=entity_type,
            entity_id=(str(entity_id) if entity_id is not None else None),
            status='queued',
            not_before=not_before,
            batch_key=(batch_key or None),
        )
        db.session.add(row)
        if commit:
            db.session.commit()
        else:
            db.session.flush()
        return row
    except Exception as exc:                                  # noqa: BLE001
        # Overwhelmingly this is the unique index refusing a duplicate,
        # which is the mechanism working.
        try:
            db.session.rollback()
        except Exception:
            pass
        log.info('not queued (%s): %r', str(exc)[:120], (subject or '')[:60])
        return None


# ── attachments ──────────────────────────────────────────────────────
def resolve_attachments(spec):
    """Turn [{'kind': ..., 'id': ...}] into bytes, at send time.

    Reading the file now rather than at queue time keeps a nine-megabyte
    row out of the database and means a file deleted in between is
    simply left out instead of failing the send.
    """
    import os

    out = []
    for item in (spec or []):
        try:
            kind, ident = item.get('kind'), item.get('id')
            path = name = ctype = None
            if kind == 'lead_attachment':
                from app import LeadAttachment, db
                row = db.session.get(LeadAttachment, ident)
                if row:
                    path, name = row.storage_path, row.filename
                    ctype = row.content_type
            elif kind == 'raw_email':
                from app import db
                from app.models.mailops import LeadRawEmail
                row = db.session.get(LeadRawEmail, ident)
                if row:
                    path = row.storage_path
                    name = (row.subject or 'original')[:80].strip() or 'original'
                    name = ''.join(c for c in name
                                   if c.isalnum() or c in ' -_()') + '.eml'
                    ctype = 'message/rfc822'
            if not path or not os.path.exists(path):
                log.info('attachment %s:%s is not on disk — left out',
                         kind, ident)
                continue
            with open(path, 'rb') as fh:
                out.append({'filename': name or 'attachment',
                            'content_type': ctype or 'application/octet-stream',
                            'content': fh.read()})
        except Exception:                                     # noqa: BLE001
            log.exception('could not read an outbox attachment: %r', item)
    return out


# ── the worker ───────────────────────────────────────────────────────
def _unstick(now):
    """Return rows abandoned by a dead worker to the queue."""
    from app import db
    from app.models.mailops import EmailOutbox
    from sqlalchemy import update

    cutoff = now - timedelta(minutes=STUCK_MINUTES)
    res = db.session.execute(
        update(EmailOutbox.__table__)
        .where(EmailOutbox.__table__.c.status == 'sending')
        .where(EmailOutbox.__table__.c.claimed_at < cutoff)
        .values(status='queued', claimed_by=None, claimed_at=None))
    db.session.commit()
    return res.rowcount or 0


def claim(limit=CLAIM_LIMIT, now=None, worker=None):
    """Take up to `limit` due rows. Returns the rows we own."""
    from app import db
    from app.models.mailops import EmailOutbox
    from app.services import leases
    from sqlalchemy import update

    now = now or datetime.utcnow()
    worker = worker or leases.me()

    due = (EmailOutbox.query
           .filter(EmailOutbox.status == 'queued',
                   EmailOutbox.not_before <= now)
           .order_by(EmailOutbox.not_before, EmailOutbox.id)
           .limit(limit).all())
    ids = [r.id for r in due]
    if not ids:
        return []

    db.session.execute(
        update(EmailOutbox.__table__)
        .where(EmailOutbox.__table__.c.id.in_(ids))
        .where(EmailOutbox.__table__.c.status == 'queued')
        .values(status='sending', claimed_by=worker, claimed_at=now))
    db.session.commit()

    # Only the rows the UPDATE actually took — anything another writer
    # got to first now has a different claimed_by.
    return (EmailOutbox.query
            .filter(EmailOutbox.id.in_(ids),
                    EmailOutbox.status == 'sending',
                    EmailOutbox.claimed_by == worker)
            .order_by(EmailOutbox.id).all())


def _group(rows):
    """Rows that should arrive as one email, grouped.

    A batch is one recipient plus one batch key. Rows without a batch
    key are their own group, which is the common case.
    """
    groups, singles = {}, []
    for row in rows:
        if row.batch_key:
            groups.setdefault((row.to_addr, row.batch_key), []).append(row)
        else:
            singles.append([row])
    return list(groups.values()) + singles


def _combined_html(rows):
    from email_ingest import notifier
    esc = notifier._esc
    parts = [
        '<div style="font-family:Arial,sans-serif;font-size:14px;color:#111">',
        f'<p style="font-weight:600;font-size:16px">{len(rows)} updates '
        f'from the CRM</p>',
    ]
    for row in rows:
        parts.append(
            '<div style="border-left:3px solid #BC1D2F;padding:4px 0 4px 12px;'
            'margin:14px 0">'
            f'<p style="margin:0 0 6px;font-weight:600">{esc(row.subject)}</p>'
            f'{row.html}</div>')
    parts.append('</div>')
    return ''.join(parts)


def _mark_sent(rows, now):
    from app import db
    for row in rows:
        row.status = 'sent'
        row.sent_at = now
        row.attempts = (row.attempts or 0) + 1
        row.last_error = None
    db.session.commit()


def _mark_failed(rows, now, error):
    from app import db
    for row in rows:
        attempts = (row.attempts or 0) + 1
        row.attempts = attempts
        row.last_error = (error or 'the mail server refused it')[:500]
        if attempts >= (row.max_attempts or len(BACKOFF_MINUTES)):
            row.status = 'failed'
            row.claimed_by = None
        else:
            idx = min(attempts - 1, len(BACKOFF_MINUTES) - 1)
            row.status = 'queued'
            row.claimed_by = None
            row.claimed_at = None
            row.not_before = now + timedelta(minutes=BACKOFF_MINUTES[idx])
    db.session.commit()


def send_group(rows, now=None, send=None):
    """Deliver one group. Returns True if it went."""
    from email_ingest import notifier

    now = now or datetime.utcnow()
    send = send or notifier.send
    lead = rows[0]
    if len(rows) == 1:
        subject, html = lead.subject, lead.html
    else:
        subject = f'{len(rows)} CRM updates — {lead.subject}'[:250]
        html = _combined_html(rows)

    attachments = []
    for row in rows:
        try:
            spec = json.loads(row.attachments_json or '[]')
        except Exception:
            spec = []
        attachments.extend(resolve_attachments(spec))

    try:
        ok = bool(send(lead.to_addr.split(', '), subject, html,
                       cc=(lead.cc_addr.split(', ') if lead.cc_addr else None),
                       attachments=attachments or None))
    except Exception as exc:                                  # noqa: BLE001
        log.exception('outbox send raised for %r', subject[:60])
        _mark_failed(rows, now, str(exc))
        return False

    if ok:
        _mark_sent(rows, now)
    else:
        _mark_failed(rows, now, 'the mail server refused it')
    return ok


def run_once(limit=CLAIM_LIMIT, now=None, send=None, use_lease=True):
    """One pass of the worker. Returns a small report."""
    from app.services import leases

    now = now or datetime.utcnow()
    report = {'claimed': 0, 'sent': 0, 'failed': 0, 'groups': 0,
              'unstuck': 0, 'skipped': 'no'}

    def _work():
        report['unstuck'] = _unstick(now)
        rows = claim(limit=limit, now=now)
        report['claimed'] = len(rows)
        for group in _group(rows):
            report['groups'] += 1
            if send_group(group, now=now, send=send):
                report['sent'] += len(group)
            else:
                report['failed'] += len(group)

    if not use_lease:
        _work()
        return report

    with leases.hold(LEASE_NAME, seconds=300) as got:
        if not got:
            report['skipped'] = 'another worker holds the lease'
            return report
        _work()
    return report


# ── health ───────────────────────────────────────────────────────────
def health(hours=24, now=None):
    """What the Email Health screen shows."""
    from app.models.mailops import EmailOutbox
    from sqlalchemy import func

    from app import db

    now = now or datetime.utcnow()
    since = now - timedelta(hours=hours)
    counts = dict(
        db.session.query(EmailOutbox.status, func.count(EmailOutbox.id))
        .group_by(EmailOutbox.status).all())
    recent = dict(
        db.session.query(EmailOutbox.status, func.count(EmailOutbox.id))
        .filter(EmailOutbox.created_at >= since)
        .group_by(EmailOutbox.status).all())
    oldest = (EmailOutbox.query
              .filter(EmailOutbox.status == 'queued')
              .order_by(EmailOutbox.not_before).first())
    failures = (EmailOutbox.query
                .filter(EmailOutbox.status == 'failed')
                .order_by(EmailOutbox.id.desc()).limit(20).all())
    return {
        'totals': counts,
        'last_hours': hours,
        'recent': recent,
        'queued': counts.get('queued', 0),
        'failed': counts.get('failed', 0),
        'oldest_queued_minutes': (
            int((now - oldest.not_before).total_seconds() // 60)
            if oldest and oldest.not_before and oldest.not_before < now else 0),
        'failures': [f.to_dict() for f in failures],
    }


def retry(ids, now=None):
    """Put failed rows back in the queue. Returns how many moved."""
    from app import db
    from app.models.mailops import EmailOutbox
    from sqlalchemy import update

    now = now or datetime.utcnow()
    res = db.session.execute(
        update(EmailOutbox.__table__)
        .where(EmailOutbox.__table__.c.id.in_(list(ids)))
        .where(EmailOutbox.__table__.c.status == 'failed')
        .values(status='queued', attempts=0, claimed_by=None,
                claimed_at=None, not_before=now, last_error=None))
    db.session.commit()
    return res.rowcount or 0


def cancel(ids):
    """Stop rows from going out at all."""
    from app import db
    from app.models.mailops import EmailOutbox
    from sqlalchemy import update

    res = db.session.execute(
        update(EmailOutbox.__table__)
        .where(EmailOutbox.__table__.c.id.in_(list(ids)))
        .where(EmailOutbox.__table__.c.status.in_(('queued', 'failed')))
        .values(status='cancelled', claimed_by=None))
    db.session.commit()
    return res.rowcount or 0
