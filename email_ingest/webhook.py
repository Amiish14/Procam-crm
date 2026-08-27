"""
v2026-08 — Microsoft Graph webhook handler for the mailbox mode.

Flow:
  1. Graph POSTs a notification to /api/email/webhook whenever a message
     is created in the Inbox.
  2. We verify the clientState matches EMAIL_WEBHOOK_SECRET.
  3. For each notification, fetch the message via Graph and call
     process_single_message() — the same code path the poll pipeline
     uses. That handles parsing, AI extract, dedup, Lead creation.
  4. Result is recorded in the EmailEvent table for admin visibility
     (RECEIVED / PROCESSED / LEAD_CREATED / SKIPPED / FAILED).
"""
from __future__ import annotations
import os
import logging
from datetime import datetime

from .graph_client import GraphClient
from .single_message import process_single_message

log = logging.getLogger(__name__)


_SELECT = ("$select=id,internetMessageId,subject,from,toRecipients,"
           "ccRecipients,receivedDateTime,conversationId,body,bodyPreview,"
           "hasAttachments")


def _get_message(graph: GraphClient, mailbox: str, message_id: str) -> dict:
    """Fetch a single message. `message_id` may be either:
      * Graph's internal id (opaque base64-ish string) — direct GET, or
      * RFC-5322 internetMessageId (looks like <abc@domain>) — $filter lookup.

    We auto-detect by looking for the '<' prefix or '@' inside the id."""
    is_rfc = message_id.startswith('<') or '@' in message_id
    if is_rfc:
        return _get_message_by_internet_id(graph, mailbox, message_id)

    path = f"/users/{mailbox}/messages/{message_id}?{_SELECT}"
    resp = graph._request('GET', path)
    if resp.status_code >= 400:
        raise RuntimeError(
            f'Graph get_message failed: HTTP {resp.status_code} — '
            f'{resp.text[:500]}'
        )
    return resp.json()


def _get_message_by_internet_id(graph: GraphClient, mailbox: str,
                                imid: str) -> dict:
    """Look up a message by RFC-5322 internetMessageId via $filter."""
    # Graph $filter needs single-quotes escaped by doubling.
    imid_esc = imid.replace("'", "''")
    path = (f"/users/{mailbox}/messages"
            f"?$filter=internetMessageId eq '{imid_esc}'"
            f"&{_SELECT}&$top=1")
    resp = graph._request('GET', path)
    if resp.status_code >= 400:
        raise RuntimeError(
            f'Graph search by internetMessageId failed: HTTP '
            f'{resp.status_code} — {resp.text[:500]}'
        )
    data = resp.json()
    vals = data.get('value') or []
    if not vals:
        raise RuntimeError(
            f'Graph could not find message with internetMessageId {imid!r} '
            f'in mailbox {mailbox!r} — it may have been deleted or moved '
            f'out of the Inbox folder.'
        )
    return vals[0]


def handle_notification(payload: dict) -> dict:
    """Called from the Flask route. Returns a stats dict."""
    from app import app, db, EmailEvent   # type: ignore

    secret_expected = os.environ.get('EMAIL_WEBHOOK_SECRET', '')
    mailbox         = os.environ.get('CRM_INBOX_EMAIL')
    if not mailbox:
        log.error('CRM_INBOX_EMAIL not set — cannot process webhook.')
        return {'error': 'not configured'}

    notifications = payload.get('value', []) if isinstance(payload, dict) else []
    if not notifications:
        return {'processed': 0, 'note': 'empty payload'}

    graph = GraphClient()
    stats = {'processed': 0, 'created': 0, 'skipped': 0, 'failed': 0}

    with app.app_context():
        for n in notifications:
            if secret_expected and n.get('clientState') != secret_expected:
                log.warning('webhook: clientState mismatch — rejecting one item')
                stats['failed'] += 1
                _log_event(db, EmailEvent, None, mailbox, 'rejected',
                           reason='clientState mismatch', payload=n)
                continue

            resource = n.get('resource') or ''
            msg_ref  = n.get('resourceData', {}).get('id')
            if not msg_ref:
                log.warning('webhook: no resourceData.id in notification')
                stats['failed'] += 1
                _log_event(db, EmailEvent, None, mailbox, 'failed',
                           reason='no message id', payload=n)
                continue

            evt = _log_event(db, EmailEvent, None, mailbox, 'received',
                             reason=None, payload=n)

            # Fetch + process
            try:
                msg = _get_message(graph, mailbox, msg_ref)
            except Exception as e:
                log.exception('webhook: fetch %s failed', msg_ref)
                evt.status = 'failed'; evt.reason = f'fetch: {e}'
                db.session.commit()
                stats['failed'] += 1
                continue

            evt.internet_message_id = msg.get('internetMessageId') or msg.get('id')
            evt.subject             = (msg.get('subject') or '')[:250]
            db.session.commit()

            result = process_single_message(graph, mailbox=mailbox, msg=msg)
            stats['processed'] += 1
            if result['status'] == 'created':
                stats['created'] += 1
                evt.status  = 'lead_created'
                evt.lead_id = result.get('lead_id')
            elif result['status'] == 'skipped':
                stats['skipped'] += 1
                evt.status = 'skipped'
                evt.reason = result.get('reason')
            else:
                stats['failed'] += 1
                evt.status = 'failed'
                evt.reason = result.get('reason')
            db.session.commit()
    return stats


def _log_event(db, EmailEvent, internet_message_id, mailbox,
               status, reason, payload):
    evt = EmailEvent(
        received_at         = datetime.utcnow(),
        mailbox             = mailbox,
        internet_message_id = internet_message_id,
        status              = status,
        reason              = reason,
        payload_json        = _short(payload) if payload else None,
    )
    db.session.add(evt); db.session.commit()
    return evt


def _short(obj) -> str:
    import json
    try:
        return json.dumps(obj)[:4000]
    except Exception:
        return str(obj)[:4000]
