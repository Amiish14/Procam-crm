"""
v2026-09-02 — Outbound notification email for the CRM.

Sends via Microsoft Graph `sendMail`, reusing the same Azure app
registration the ingest already uses. Mail goes out from the leads mailbox
(NOTIFY_FROM, defaulting to CRM_INBOX_EMAIL = leads@procamgroup.in).

IMPORTANT — this needs the **Mail.Send** application permission granted on
the app registration, which is a separate grant from Mail.Read. Run

    python scripts/2026_09_02_test_notification.py --to you@procamgroup.in

to check; a 403 means IT still has to grant and admin-consent Mail.Send.

Everything here fails soft: if the permission is missing, or NOTIFY_ENABLED
is off, or the recipient has no address, we log and return False. A
notification must never break the action that triggered it — nobody should
lose a lead assignment because the mail server hiccuped.

Env:
    NOTIFY_ENABLED   'true' (default) — master switch
    NOTIFY_FROM      sender mailbox; defaults to CRM_INBOX_EMAIL
    CRM_BASE_URL     public URL of the CRM, used to build deep links,
                     e.g. https://procamlogitech.com/CRM
"""
from __future__ import annotations

import logging
import os
from typing import Optional, Sequence

log = logging.getLogger(__name__)


def is_enabled() -> bool:
    raw = os.environ.get('NOTIFY_ENABLED')
    if raw is None or raw == '':
        return True
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


def sender() -> str:
    from . import service as _mail
    return (os.environ.get('NOTIFY_FROM') or _mail.crm_inbox_email() or '').strip()


def base_url() -> str:
    return (os.environ.get('CRM_BASE_URL') or '').strip().rstrip('/')


def lead_link(lead_id: int) -> str:
    """Deep link for a lead. /leads/<id> sends a signed-in user straight to
    the open lead, and anyone else to the login page carrying ?next=, so
    they land on the lead rather than a generic dashboard."""
    root = base_url()
    return f'{root}/leads/{int(lead_id)}' if root else f'/leads/{int(lead_id)}'


#: Microsoft Graph accepts attachments inline on sendMail only while the
#: whole message stays small; past that the API wants an upload session
#: against a draft, which this transport does not do. Anything over the
#: budget is left out and named in the email instead of being sent.
MAX_INLINE_ATTACHMENT_BYTES = 3 * 1024 * 1024


def send(to: Sequence[str] | str, subject: str, html: str,
         cc: Optional[Sequence[str]] = None,
         attachments: Optional[Sequence[dict]] = None,
         text: Optional[str] = None) -> bool:
    """Send one HTML email. Returns True on success, False on any failure.

    `attachments` is a list of {'filename', 'content_type', 'content'}
    where content is raw bytes. Files are carried inline; see
    MAX_INLINE_ATTACHMENT_BYTES.

    Never raises — callers treat notification as best-effort.
    """
    if not is_enabled():
        log.info('notifications disabled — not sending %r', subject[:60])
        return False

    recipients = [to] if isinstance(to, str) else list(to or [])
    recipients = [r.strip() for r in recipients if r and '@' in r]

    # The hard rail: the CRM never writes to an address outside Procam.
    # Checked here, in front of the only transport, so no call site can
    # route around it. See app/services/mail_policy.py.
    try:
        from app.services import mail_policy
        recipients, refused = mail_policy.check(recipients)
        if refused:
            log.error('REFUSED external recipient(s) for %r: %s — %s',
                      subject[:60], ', '.join(refused), mail_policy.describe())
        if cc:
            cc, cc_refused = mail_policy.check(list(cc))
            if cc_refused:
                log.error('REFUSED external cc for %r: %s',
                          subject[:60], ', '.join(cc_refused))
    except ImportError:                                       # pragma: no cover
        log.exception('mail policy unavailable — refusing to send')
        return False

    if not recipients:
        log.info('no valid recipient for %r — skipping', subject[:60])
        return False

    frm = sender()
    if not frm:
        log.error('NOTIFY_FROM / CRM_INBOX_EMAIL not set — cannot send mail')
        return False

    def _addrs(items):
        return [{'emailAddress': {'address': a}} for a in items]

    payload = {
        'message': {
            'subject': subject[:250],
            'body': {'contentType': 'HTML', 'content': html},
            'toRecipients': _addrs(recipients),
        },
        'saveToSentItems': True,
    }
    if attachments:
        payload['message']['attachments'] = _attachment_payload(
            attachments, subject)
    if cc:
        cc_list = [c.strip() for c in cc if c and '@' in c]
        if cc_list:
            payload['message']['ccRecipients'] = _addrs(cc_list)

    try:
        from .graph_client import GraphClient
        graph = GraphClient()
        resp = graph._request('POST', f'/users/{frm}/sendMail', json_body=payload)
    except Exception as e:                                        # noqa: BLE001
        log.exception('notification send failed (%r): %s', subject[:60], e)
        return False

    if resp.status_code in (200, 202):
        log.info('sent %r to %s', subject[:60], ', '.join(recipients))
        return True

    if resp.status_code == 403:
        log.error(
            'Graph refused sendMail (403). The app registration is missing '
            'the Mail.Send application permission — IT must grant it and '
            'give admin consent. Response: %s', resp.text[:300])
    else:
        log.error('sendMail failed: HTTP %s — %s',
                  resp.status_code, resp.text[:300])
    return False


def _attachment_payload(attachments, subject=''):
    """Graph fileAttachment objects, inside the size budget.

    Returns the ones that fit. A file that does not fit is logged and
    dropped — the email still goes, because the recipient losing the
    whole notification is worse than losing one attachment, and the
    body always carries a link to the record where the file lives.
    """
    import base64

    out, used = [], 0
    for att in attachments:
        try:
            content = att.get('content')
            if not content:
                continue
            name = (att.get('filename') or 'attachment')[:200]
            # base64 inflates by 4/3; budget against what goes on the wire.
            encoded_size = (len(content) + 2) // 3 * 4
            if used + encoded_size > MAX_INLINE_ATTACHMENT_BYTES:
                log.info('attachment %s left out of %r — over the %d byte '
                         'inline budget', name, subject[:40],
                         MAX_INLINE_ATTACHMENT_BYTES)
                continue
            used += encoded_size
            out.append({
                '@odata.type': '#microsoft.graph.fileAttachment',
                'name': name,
                'contentType': att.get('content_type')
                or 'application/octet-stream',
                'contentBytes': base64.b64encode(content).decode('ascii'),
            })
        except Exception:                                     # noqa: BLE001
            log.exception('could not encode an attachment for %r', subject[:40])
    return out


# ─── Templates ────────────────────────────────────────────────────────
_SHELL = """\
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
            color:#1a1917;font-size:14px;line-height:1.6;max-width:620px;">
  <div style="border-left:3px solid #CC1E2E;padding-left:14px;margin-bottom:20px;">
    <div style="font-size:11px;letter-spacing:.09em;text-transform:uppercase;
                color:#8A857F;font-weight:600;">Procam CRM</div>
    <div style="font-size:19px;font-weight:600;margin-top:2px;">{heading}</div>
  </div>
  {body}
  <div style="margin-top:26px;padding-top:14px;border-top:1px solid #E0DDDA;
              font-size:11.5px;color:#8A857F;">
    Automated message from the Procam CRM. Replies are not monitored.
  </div>
</div>"""

_BTN = ('<a href="{url}" style="display:inline-block;background:#CC1E2E;color:#fff;'
        'text-decoration:none;padding:11px 22px;border-radius:6px;font-weight:600;'
        'font-size:13.5px;">{label}</a>')


def _row(label: str, value: str) -> str:
    if not value:
        return ''
    return (f'<tr><td style="padding:5px 16px 5px 0;color:#6B6762;'
            f'white-space:nowrap;vertical-align:top;">{label}</td>'
            f'<td style="padding:5px 0;font-weight:500;">{value}</td></tr>')


def _esc(v) -> str:
    from html import escape
    return escape(str(v or ''))


def lead_assigned_html(lead, assigned_by: str = '', attachments=None) -> str:
    """Body for the 'a lead was assigned to you' notification.

    `attachments` is the outbox reference list, used only to say in the
    body what is attached. A recipient who sees "the original request
    is attached" and finds nothing has been told a lie by the software,
    so the line is written from the same list the files come from.
    """
    summary = ''
    try:
        import json
        x = json.loads(lead.email_extracted_json or '{}') or {}
        summary = x.get('one_line_summary') or ''
    except Exception:
        pass

    rows = (
        _row('Company', _esc(lead.company)) +
        _row('Contact', _esc(lead.pic)) +
        _row('Email', _esc(lead.email)) +
        _row('Phone', _esc(lead.phone)) +
        _row('Vertical', _esc(lead.procam_vertical)) +
        _row('Stage', _esc(lead.stage)) +
        _row('Assigned by', _esc(assigned_by))
    )
    body = ''
    if summary:
        body += (f'<div style="background:#FAFAFA;border:1px solid #E0DDDA;'
                 f'border-radius:8px;padding:13px 16px;margin-bottom:18px;">'
                 f'{_esc(summary)}</div>')
    body += f'<table style="border-collapse:collapse;font-size:13.5px;">{rows}</table>'
    body += _attached_note(attachments)
    body += ('<div style="margin-top:22px;">'
             + _BTN.format(url=lead_link(lead.id), label='Open this lead')
             + '</div>')
    return _SHELL.format(heading='A lead has been assigned to you', body=body)


def _attached_note(attachments) -> str:
    """One line naming what is attached, or nothing."""
    refs = list(attachments or [])
    if not refs:
        return ''
    has_original = any(r.get('kind') == 'raw_email' for r in refs)
    files = sum(1 for r in refs if r.get('kind') == 'lead_attachment')
    bits = []
    if has_original:
        bits.append('the client\'s original email')
    if files:
        bits.append(f'{files} attached file' + ('s' if files != 1 else ''))
    if not bits:
        return ''
    what = ' and '.join(bits)
    return (f'<div style="margin-top:18px;padding:11px 14px;background:#FFF7F7;'
            f'border:1px solid #F0D7D9;border-radius:8px;font-size:13px;">'
            f'Attached to this message: {what}. Forward it as it stands — '
            f'no need to ask anyone for the original.</div>')


def notify_lead_assigned(lead, employee, assigned_by: str = '') -> bool:
    """Best-effort 'you have a new lead' email. Safe to call inline."""
    try:
        addr = (getattr(employee, 'email', '') or '').strip()
        if not addr:
            log.info('employee %s has no email on file — no notification sent',
                     getattr(employee, 'emp_code', '?'))
            return False
        subject = f'New lead assigned: {(lead.company or "Untitled")[:80]}'
        return send(addr, subject, lead_assigned_html(lead, assigned_by))
    except Exception:                                             # noqa: BLE001
        log.exception('notify_lead_assigned failed for lead %s',
                      getattr(lead, 'id', '?'))
        return False
