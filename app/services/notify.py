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

import contextlib
import threading
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



# ── bulk work ────────────────────────────────────────────────────────
# A bulk assignment of four hundred leads must not be four hundred
# emails. One person receiving four hundred notifications reads none
# of them and turns the rest off, which costs the CRM every
# notification it will ever send that person.
#
# Two modes, held per thread so one request's bulk run cannot silence
# another's:
#
#   bulk()    collect, then send one summary per recipient
#   silent()  send nothing at all — for a backfill, where the
#             notifications would describe work that happened months
#             ago
_local = threading.local()


def _mode():
    return getattr(_local, 'mode', None)


@contextlib.contextmanager
def bulk(what='records', url=None):
    """Collect notifications and send one summary each at the end."""
    previous, previous_box = _mode(), getattr(_local, 'box', None)
    _local.mode, _local.box = 'bulk', {}
    try:
        yield _local.box
    finally:
        box = _local.box
        _local.mode, _local.box = previous, previous_box
        try:
            _send_summaries(box, what, url)
        except Exception:
            _logger().exception('could not send the bulk summaries')


@contextlib.contextmanager
def silent():
    """Send nothing. Returns what would have been sent, for a report."""
    previous, previous_box = _mode(), getattr(_local, 'box', None)
    _local.mode, _local.box = 'silent', {}
    try:
        yield _local.box
    finally:
        _local.mode, _local.box = previous, previous_box


def _collect(user_code, kind, title, body, url):
    box = getattr(_local, 'box', None)
    if box is None:
        return
    box.setdefault(user_code, []).append(
        {'kind': kind, 'title': title, 'body': body, 'url': url})


def _send_summaries(box, what, url):
    """One email per person, listing what happened to their records."""
    for user_code, items in (box or {}).items():
        if not items:
            continue
        if len(items) == 1:
            one = items[0]
            send(user_code, kind=one['kind'], title=one['title'],
                 body=one['body'], url=one['url'], email=True,
                 dedupe=False)
            continue
        shown = items[:50]
        more = len(items) - len(shown)
        # The plain-text body and the HTML are built separately. A
        # newline-joined list dropped into a <p> renders as one long
        # run-on line, which is how a summary of forty things becomes
        # unreadable.
        lines = '\n'.join(f'• {i["title"]}' for i in shown)
        if more:
            lines += f'\n… and {more} more'
        send(user_code, kind='bulk_summary',
             event_key='bulk.summary',
             title=f'{len(items)} {what} were updated',
             body=f'A bulk update touched {len(items)} of your '
                  f'{what}:\n{lines}',
             email_html=_summary_html(len(items), what, shown, more, url),
             url=url, email=True, dedupe=False)


def _summary_html(total, what, shown, more, url):
    from email_ingest import notifier
    esc = notifier._esc

    rows = ''.join(
        f'<li style="margin:3px 0">{esc(i["title"])}</li>' for i in shown)
    tail = (f'<p style="color:#6B6762;font-size:12.5px">… and {more} '
            f'more.</p>' if more else '')
    link = ''
    if url:
        base = notifier.base_url()
        href = f'{base}{url}' if url.startswith('/') else url
        link = (f'<p style="margin:18px 0"><a href="{esc(href)}" '
                f'style="background:#BC1D2F;color:#fff;padding:10px 18px;'
                f'border-radius:6px;text-decoration:none;font-weight:600">'
                f'Open the CRM</a></p>')
    return (f'<div style="font-family:Arial,sans-serif;font-size:14px;'
            f'color:#111">'
            f'<p style="font-weight:600;font-size:16px">A bulk update '
            f'touched {total} of your {esc(what)}</p>'
            f'<ul style="padding-left:18px;margin:10px 0">{rows}</ul>'
            f'{tail}{link}</div>')


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

    # Inside a bulk run or a backfill, nothing goes out one record at
    # a time. See `bulk()` and `silent()`.
    mode = _mode()
    if mode is not None:
        _collect(user_code, kind, title, body, url)
        out['suppressed'] = True
        out['collected'] = True
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
                    from app.services import mailer
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
                    out['emailed'] = bool(mailer.send(
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


# ── one email, several people ────────────────────────────────────────
def addresses_for(user_codes, *, event_key=None):
    """(addresses, problems) for a list of employee codes.

    `problems` is [(code, reason)] — no address on file, not an active
    employee, muted this event. All three are ordinary outcomes that
    an administrator should be able to see, which is why they are
    returned rather than logged and dropped.
    """
    from app import Employee
    from app.services import notify_prefs

    addresses, problems = [], []
    for code in dict.fromkeys(c for c in (user_codes or []) if c):
        code = code.strip().upper()
        emp = Employee.query.filter_by(emp_code=code).first()
        if emp is None:
            problems.append((code, 'not an employee on file'))
            continue
        if emp.is_active is False:
            problems.append((code, 'no longer active'))
            continue
        address = (getattr(emp, 'email', '') or '').strip()
        if not address:
            problems.append((code, 'no email address on file'))
            continue
        if event_key and not notify_prefs.wants(code, event_key):
            problems.append((code, 'has muted this notification'))
            continue
        if address not in addresses:
            addresses.append(address)
    return addresses, problems


def send_policy(to_codes, cc_codes=(), *, kind, title, body='', url=None,
                entity_type=None, entity_id=None, email=False,
                email_html=None, actor=None, dedupe=True, in_app=True,
                event_key=None, attachments=None, batch_key=None,
                urgent=None, policy_version=None):
    """Tell several people about one thing, as one email.

    The bell is per person, because a notification is read and
    dismissed by one person. The email is not: it goes out once, with
    everybody on the To line and the CC list, so that

        - a reply reaches the thread rather than one recipient,
        - everyone can see who else knows, which is the thing four
          separate emails destroy, and
        - the four emails are one email.

    Returns {'bell': {code: result}, 'email': {...}}. Never raises.
    """
    to_codes = [c.strip().upper() for c in (to_codes or []) if c and c.strip()]
    cc_codes = [c.strip().upper() for c in (cc_codes or []) if c and c.strip()]
    cc_codes = [c for c in cc_codes if c not in to_codes]

    out = {'bell': {}, 'email': {'queued': False, 'sent': False,
                                 'to': [], 'cc': [], 'problems': [],
                                 'error': None}}

    # The bell row for everyone who is told at all — a person on the
    # CC line is being kept informed, and the place the CRM keeps
    # people informed is the bell.
    if in_app:
        for code in to_codes + cc_codes:
            out['bell'][code] = send(
                code, kind=kind, title=title, body=body, url=url,
                entity_type=entity_type, entity_id=entity_id,
                email=False, actor=actor, dedupe=dedupe, in_app=True,
                event_key=event_key)

    if not email:
        return out

    mode = _mode()
    if mode is not None:
        # Inside a bulk run or a backfill. `send` above has already
        # collected the lines for the summary; there is nothing to
        # email now.
        out['email']['error'] = 'collected into a summary'
        return out

    try:
        to_addr, to_problems = addresses_for(to_codes, event_key=event_key)
        cc_addr, cc_problems = addresses_for(cc_codes, event_key=event_key)
        cc_addr = [a for a in cc_addr if a not in to_addr]
        out['email']['to'] = to_addr
        out['email']['cc'] = cc_addr
        out['email']['problems'] = to_problems + cc_problems

        if not to_addr:
            # Everybody on the To line is unreachable. Promoting a CC
            # recipient would send a client-facing document to
            # somebody who was only being kept informed, so the
            # message is not sent and the reason is recorded.
            out['email']['error'] = ('no reachable recipient on the To line'
                                     if (to_codes or cc_codes)
                                     else 'no recipients')
            return out

        html = email_html or _plain_html(title, body, url)
        from app.services import flags
        if flags.on('FEATURE_EMAIL_NOTIFY'):
            from app.services import notify_prefs, outbox

            # One email cannot honour four different sets of quiet
            # hours. It waits only if everybody on it is in theirs,
            # and then only until the first of them is up.
            when, _why = notify_prefs.release_at_for_all(
                to_codes, event_key=event_key or kind, urgent=urgent)

            row = outbox.enqueue(
                to=to_addr, subject=title, html=html, text=body or None,
                cc=cc_addr or None,
                dedupe_key=_dedupe_key(','.join(to_codes),
                                       event_key or kind, entity_type,
                                       entity_id),
                event_key=event_key or kind,
                user_code=(to_codes[0] if len(to_codes) == 1 else None),
                attachments=attachments, entity_type=entity_type,
                entity_id=entity_id, batch_key=batch_key, urgent=urgent,
                not_before=when,
                policy_version=policy_version)
            out['email']['queued'] = row is not None
            if row is None:
                out['email']['error'] = ('already queued, muted, or not an '
                                         'internal address')
        else:
            from app.services import mailer
            files = None
            if attachments:
                from app.services import outbox
                files = outbox.resolve_attachments(attachments) or None
            extra = {'attachments': files} if files else {}
            if cc_addr:
                extra['cc'] = cc_addr
            result = mailer.send_detail(to_addr, title, html, **extra)
            # A test double, and any caller that wrapped the transport
            # before this release, returns a bare bool. Accept both.
            if isinstance(result, dict):
                ok, why = bool(result.get('ok')), result.get('error')
            else:
                ok, why = bool(result), None
            out['email']['sent'] = ok
            if not ok:
                out['email']['error'] = why or 'the mail server refused it'
    except Exception as exc:                                  # noqa: BLE001
        _logger().exception('could not send a policy email for %r', event_key)
        out['email']['error'] = str(exc)[:200]
    return out
