"""
One way to tell somebody something.

Before this, an in-app notification was written in two places and email
went out from exactly one (lead assignment). Anything new that wanted to
reach a person copied that code. This module is the single entry point:
callers say who, what and which channels, and it writes the in-app
notification, sends the email through the existing Graph transport, and
records that it happened.

It never raises. A notification that cannot be delivered must not roll
back the work that triggered it — the caller's save is the thing that
matters, and the failure is logged and reported back in the result.

Duplicate suppression: the same kind, for the same person, about the
same record, within `DEDUPE_MINUTES`, is not sent twice. Reminders and
escalations repeat on purpose, so the window is short enough to catch a
double-click and nothing more.
"""
from __future__ import annotations

from datetime import datetime, timedelta

#: Suppress an identical notification repeated inside this window.
DEDUPE_MINUTES = 10


def _logger():
    try:
        from flask import current_app
        return current_app.logger
    except Exception:                                   # pragma: no cover
        import logging
        return logging.getLogger(__name__)


def _recent_duplicate(user_id, kind, entity_type, entity_id, since):
    from app.models.notification import Notification
    q = Notification.query.filter(
        Notification.user_id == user_id,
        Notification.kind == kind,
        Notification.created_at >= since)
    if entity_type:
        q = q.filter(Notification.entity_type == entity_type)
    if entity_id is not None:
        q = q.filter(Notification.entity_id == str(entity_id))
    return q.first()


def send(user_code, *, kind, title, body='', url=None, entity_type=None,
         entity_id=None, email=False, email_html=None, actor=None,
         audit_action=None, reason=None, commit=True, dedupe=True,
         in_app=True, event_key=None, attachments=None, batch_key=None,
         urgent=None):
    """Notify one person. Returns what actually happened.

    {'notified': bool, 'emailed': bool, 'suppressed': bool,
     'error': str|None}

    `in_app=False` sends the email without leaving a bell notification.
    It exists for the scheduled reports: a digest is a summary of
    things that already have their own notifications, and adding a
    daily "your report was sent" row would bury them. With no row
    written there is nothing to dedupe against, so the caller owns
    repetition — for the reports, the timer does.
    """
    from app import db, Employee
    from app.models.notification import Notification
    from app.services import audit

    out = {'notified': False, 'emailed': False, 'suppressed': False,
           'queued': False, 'error': None}
    user_code = (user_code or '').strip().upper()
    if not user_code:
        out['error'] = 'no recipient'
        return out
    try:
        if dedupe and in_app:
            since = datetime.utcnow() - timedelta(minutes=DEDUPE_MINUTES)
            if _recent_duplicate(user_code, kind, entity_type, entity_id, since):
                out['suppressed'] = True
                return out
        if in_app:
            row = Notification(
                user_id=user_code, kind=kind, title=title[:200],
                body=(body or '')[:2000], entity_type=entity_type,
                entity_id=(str(entity_id) if entity_id is not None else None),
                action_url=url)
            db.session.add(row)
            out['notified'] = True
        if audit_action:
            audit.record(audit_action, entity_type or 'notification',
                         entity_id, new={'to': user_code, 'kind': kind,
                                         'title': title[:200]},
                         actor=actor, reason=reason)
        if commit:
            db.session.commit()
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        _logger().exception('could not write a notification for %s', user_code)
        out['error'] = str(exc)[:200]
        return out

    if email:
        try:
            emp = Employee.query.filter_by(emp_code=user_code).first()
            address = (getattr(emp, 'email', '') or '').strip()
            if not address:
                out['error'] = 'no email address on file'
            else:
                html = email_html or _plain_html(title, body, url)
                from app.services import flags
                if flags.on('FEATURE_EMAIL_NOTIFY'):
                    # Queued, not sent. The worker owns delivery, the
                    # retry and the record of what happened; the caller
                    # owns none of it and waits for none of it.
                    from app.services import outbox
                    row = outbox.enqueue(
                        to=address, subject=title, html=html,
                        text=body or None,
                        dedupe_key=_dedupe_key(user_code, event_key or kind,
                                               entity_type, entity_id),
                        event_key=event_key or kind, user_code=user_code,
                        attachments=attachments, entity_type=entity_type,
                        entity_id=entity_id, batch_key=batch_key,
                        urgent=urgent)
                    out['queued'] = row is not None
                    out['emailed'] = row is not None
                    if row is None:
                        out['error'] = ('already queued, muted, or not an '
                                        'internal address')
                else:
                    from email_ingest import notifier
                    files = None
                    if attachments:
                        from app.services import outbox
                        files = outbox.resolve_attachments(attachments) or None
                    # Only pass `attachments` when there are some: the
                    # direct path is the one that existed before this
                    # release, and callers that wrap the transport
                    # should not have to learn a new signature to keep
                    # working.
                    extra = {'attachments': files} if files else {}
                    out['emailed'] = bool(notifier.send(
                        address, title, html, **extra))
                    if not out['emailed']:
                        out['error'] = 'the mail server refused it'
        except Exception as exc:
            _logger().exception('could not email %s', user_code)
            out['error'] = str(exc)[:200]
    return out


def _dedupe_key(user_code, event_key, entity_type, entity_id):
    """The same message, to the same person, about the same record,
    inside the same ten-minute window, is one message.

    The window is the same one the in-app dedupe uses, so the bell and
    the inbox agree about what counts as a repeat.
    """
    bucket = int(datetime.utcnow().timestamp() // (DEDUPE_MINUTES * 60))
    return f'{event_key}:{user_code}:{entity_type or "-"}:' \
           f'{entity_id if entity_id is not None else "-"}:{bucket}'


def send_many(user_codes, **kwargs):
    """The same message to several people. Returns {code: result}."""
    return {code: send(code, **kwargs) for code in dict.fromkeys(user_codes)
            if code}


def _plain_html(title, body, url):
    """A plain, readable email for callers that do not bring their own."""
    from email_ingest import notifier
    esc = notifier._esc
    link = ''
    if url:
        base = notifier.base_url()
        href = f'{base}{url}' if url.startswith('/') else url
        link = (f'<p style="margin:18px 0"><a href="{esc(href)}" '
                f'style="background:#BC1D2F;color:#fff;padding:10px 18px;'
                f'border-radius:6px;text-decoration:none;font-weight:600">'
                f'Open the CRM</a></p>')
    return (f'<div style="font-family:Arial,sans-serif;font-size:14px;'
            f'color:#111"><p style="font-weight:600;font-size:16px">'
            f'{esc(title)}</p><p>{esc(body)}</p>{link}</div>')
