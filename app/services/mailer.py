"""
One way out of the building.

Every email the CRM sends goes through `send()`: password reset,
notifications, the daily brief, the weekly pack, admin reports. One
service, so the sender address, the internal-only rule and the
transport are decided once.

Why this exists rather than the Graph sender it wraps: **the CRM has
never successfully sent an email.** Graph `sendMail` needs the
Mail.Send application permission, the token has only Mail.Read, and
every send since the feature was built has been refused with a 403.
See `docs/notifications_diagnosis.md`.

So this speaks two transports and prefers whichever is configured:

    smtp   — MAIL_SERVER and friends. The same credentials the TMS
             already sends with, which is why the brief names
             tms@procamgroup.in. Needs no Microsoft permission.
    graph  — the existing Microsoft Graph sender, kept because it
             works the moment Mail.Send is granted and because it
             files a copy in the mailbox's Sent Items.

`CRM_MAIL_TRANSPORT` picks one explicitly. Unset, it uses SMTP when
`MAIL_SERVER` is configured and Graph otherwise — so putting the
credentials in `.env` is the whole deployment step, and taking them
out again is the whole rollback.

Nothing here decides *whether* a person should be emailed. That is
`notify.py` and the preferences. This decides only how the bytes
leave.
"""
from __future__ import annotations

import logging
import os
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr

log = logging.getLogger(__name__)

SMTP = 'smtp'
GRAPH = 'graph'

#: The display name on CRM mail. A notification from a bare address
#: reads like spam; one from "Procam TMS" reads like the wrong system.
DEFAULT_FROM_NAME = 'Procam CRM'

#: Attachments are carried as-is over SMTP. The cap is about the
#: receiving mailbox, not about us: most corporate mailboxes refuse
#: above 25 MB and bounce the lot, so a large original is left out and
#: named in the body instead.
MAX_TOTAL_ATTACHMENT_BYTES = 15 * 1024 * 1024


# ── configuration ────────────────────────────────────────────────────
def _env(name, default=''):
    return (os.environ.get(name) or default).strip()


def smtp_configured():
    return bool(_env('MAIL_SERVER'))


def transport():
    """Which transport is in force, as a string."""
    chosen = _env('CRM_MAIL_TRANSPORT').lower()
    if chosen in (SMTP, GRAPH):
        return chosen
    return SMTP if smtp_configured() else GRAPH


def sender():
    """(display name, address) CRM mail goes out as."""
    raw = _env('CRM_MAIL_FROM') or _env('MAIL_DEFAULT_SENDER')
    if raw:
        name, addr = parseaddr(raw)
        if addr:
            return (name or DEFAULT_FROM_NAME), addr
    # Fall back to whatever the Graph path was using, so an install
    # that has not set the new variables behaves as it did.
    from email_ingest import notifier
    return DEFAULT_FROM_NAME, notifier.sender()


def describe():
    """One line for the health screen and the runbook."""
    name, addr = sender()
    which = transport()
    if which == SMTP:
        return (f'SMTP via {_env("MAIL_SERVER")}:{_env("MAIL_PORT", "587")} '
                f'as {formataddr((name, addr))}')
    return (f'Microsoft Graph as {addr} — needs the Mail.Send grant; '
            f'set MAIL_SERVER to use SMTP instead')


def ready():
    """Can anything be sent at all? (bool, why not)"""
    from email_ingest import notifier

    if not notifier.is_enabled():
        return False, 'NOTIFY_ENABLED is off'
    _name, addr = sender()
    if not addr:
        return False, 'no sender address — set CRM_MAIL_FROM'
    if transport() == SMTP and not smtp_configured():
        return False, 'CRM_MAIL_TRANSPORT=smtp but MAIL_SERVER is unset'
    return True, ''


# ── the one entry point ──────────────────────────────────────────────
def send(to, subject, html, *, cc=None, text=None, attachments=None,
         reply_to=None, headers=None):
    """Send one message. True if it went. Never raises.

    `attachments` is [{'filename', 'content_type', 'content': bytes}].
    `text` is the plain-text alternative; one is generated from the
    HTML when it is not given, because a message with no text part
    scores as spam and reads as nothing in a text client.
    """
    from email_ingest import notifier

    if not notifier.is_enabled():
        log.info('notifications disabled — not sending %r', subject[:60])
        return False

    recipients = [to] if isinstance(to, str) else list(to or [])
    recipients = [r.strip() for r in recipients if r and '@' in r]
    cc_list = [c.strip() for c in (cc or []) if c and '@' in c]

    # The hard rail, in front of every transport. Nothing the CRM
    # sends is meant for a customer, and some of it carries a client's
    # own documents.
    from app.services import mail_policy
    recipients, refused = mail_policy.check(recipients)
    cc_list, cc_refused = mail_policy.check(cc_list)
    if refused or cc_refused:
        log.error('REFUSED external recipient(s) for %r: %s',
                  subject[:60], ', '.join(refused + cc_refused))
    if not recipients:
        log.info('no internal recipient for %r — skipping', subject[:60])
        return False

    which = transport()
    try:
        if which == SMTP:
            return _send_smtp(recipients, subject, html, cc_list, text,
                              attachments, reply_to, headers)
        # Only pass what there is. The Graph sender's signature grew
        # over time and plenty of callers and test doubles still wrap
        # the three-argument form; handing them keywords they do not
        # accept turns a working send into a TypeError.
        extra = {}
        if cc_list:
            extra['cc'] = cc_list
        if attachments:
            extra['attachments'] = attachments
        # One recipient goes as a bare string, which is the shape
        # every existing caller of the Graph sender passes and every
        # test double expects. Normalising to a list here would be
        # tidier and would quietly change what they all receive.
        one = recipients[0] if len(recipients) == 1 else recipients
        return notifier.send(one, subject, html, **extra)
    except Exception:                                         # noqa: BLE001
        log.exception('send failed over %s for %r', which, subject[:60])
        return False


def _send_smtp(recipients, subject, html, cc_list, text, attachments,
               reply_to, headers):
    import smtplib

    name, addr = sender()
    msg = EmailMessage()
    msg['Subject'] = (subject or '')[:250]
    msg['From'] = formataddr((name, addr))
    msg['To'] = ', '.join(recipients)
    if cc_list:
        msg['Cc'] = ', '.join(cc_list)
    if reply_to:
        msg['Reply-To'] = reply_to
    msg['Message-ID'] = make_msgid(domain=addr.rsplit('@', 1)[-1])
    # Marks this as automated so a mail client does not reply to it and
    # an out-of-office does not bounce back at the leads mailbox.
    msg['Auto-Submitted'] = 'auto-generated'
    for key, value in (headers or {}).items():
        msg[key] = value

    msg.set_content(text or _text_from(html))
    msg.add_alternative(html or '', subtype='html')
    _attach(msg, attachments, subject)

    host = _env('MAIL_SERVER')
    port = int(_env('MAIL_PORT', '587') or 587)
    timeout = int(_env('MAIL_TIMEOUT', '20') or 20)
    use_ssl = _env('MAIL_USE_SSL').lower() in ('1', 'true', 'yes', 'on')
    use_tls = (_env('MAIL_USE_TLS', 'true').lower()
               in ('1', 'true', 'yes', 'on'))

    server = (smtplib.SMTP_SSL(host, port, timeout=timeout) if use_ssl
              else smtplib.SMTP(host, port, timeout=timeout))
    try:
        if use_tls and not use_ssl:
            server.starttls()
        username = _env('MAIL_USERNAME')
        if username:
            server.login(username, os.environ.get('MAIL_PASSWORD') or '')
        server.send_message(msg, to_addrs=recipients + cc_list)
    finally:
        try:
            server.quit()
        except Exception:
            pass
    log.info('sent %r to %s over SMTP', (subject or '')[:60],
             ', '.join(recipients))
    return True


def _attach(msg, attachments, subject=''):
    """Add what fits inside the budget; name what does not."""
    used = 0
    for att in (attachments or []):
        try:
            content = att.get('content')
            if not content:
                continue
            if used + len(content) > MAX_TOTAL_ATTACHMENT_BYTES:
                log.info('attachment %s left out of %r — over the %d byte '
                         'budget', att.get('filename'), (subject or '')[:40],
                         MAX_TOTAL_ATTACHMENT_BYTES)
                continue
            used += len(content)
            ctype = (att.get('content_type')
                     or 'application/octet-stream').split(';')[0]
            maintype, _, subtype = ctype.partition('/')
            msg.add_attachment(content, maintype=maintype or 'application',
                               subtype=subtype or 'octet-stream',
                               filename=(att.get('filename')
                                         or 'attachment')[:200])
        except Exception:                                     # noqa: BLE001
            log.exception('could not attach %r', att.get('filename'))


def _text_from(html):
    """A readable plain-text version of an HTML body.

    Not a renderer — enough that the message is not empty in a text
    client and does not score as spam for having no text part.
    """
    import re
    from html import unescape

    body = re.sub(r'(?is)<(script|style).*?</\1>', ' ', html or '')
    body = re.sub(r'(?i)<br\s*/?>', '\n', body)
    body = re.sub(r'(?i)</(p|div|tr|h[1-6]|li)>', '\n', body)
    body = re.sub(r'<[^>]+>', ' ', body)
    body = unescape(body)
    body = re.sub(r'[ \t]+', ' ', body)
    body = re.sub(r'\n\s*\n\s*\n+', '\n\n', body)
    return body.strip() or '(no text content)'


def send_test(to):
    """A real send, for the Email Log's "send a test to me" button."""
    name, addr = sender()
    which = transport()
    html = (f'<div style="font-family:Arial,sans-serif;font-size:14px">'
            f'<p>This is a test from the Procam CRM.</p>'
            f'<p>Transport: <strong>{which}</strong><br>'
            f'From: <strong>{formataddr((name, addr))}</strong></p>'
            f'<p>If you are reading this, CRM email works.</p></div>')
    return send(to, 'Procam CRM — test message', html)
