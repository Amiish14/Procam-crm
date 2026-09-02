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


def send(to: Sequence[str] | str, subject: str, html: str,
         cc: Optional[Sequence[str]] = None) -> bool:
    """Send one HTML email. Returns True on success, False on any failure.

    Never raises — callers treat notification as best-effort.
    """
    if not is_enabled():
        log.info('notifications disabled — not sending %r', subject[:60])
        return False

    recipients = [to] if isinstance(to, str) else list(to or [])
    recipients = [r.strip() for r in recipients if r and '@' in r]
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


def lead_assigned_html(lead, assigned_by: str = '') -> str:
    """Body for the 'a lead was assigned to you' notification."""
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
    body += ('<div style="margin-top:22px;">'
             + _BTN.format(url=lead_link(lead.id), label='Open this lead')
             + '</div>')
    return _SHELL.format(heading='A lead has been assigned to you', body=body)


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
