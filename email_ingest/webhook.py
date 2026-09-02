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
import hmac
import os
import logging
import re
from datetime import datetime

from .graph_client import GraphClient
from .single_message import process_single_message

log = logging.getLogger(__name__)


_SELECT = ("$select=id,internetMessageId,subject,from,toRecipients,"
           "ccRecipients,receivedDateTime,conversationId,body,bodyPreview,"
           "hasAttachments")

# Graph writes the mailbox into a notification's `resource` in more than one
# shape, and not the shape the subscription was created with:
#     /users/leads@procamgroup.in/mailFolders('Inbox')/messages   (UPN)
#     Users/9a0b6a4e-0423-.../Messages/AAMkAGVj...                (object id)
# — varying case, sometimes without the leading slash. So parse out the user
# segment and compare it against every identifier the mailbox is known by.
_RESOURCE_USER_RE = re.compile(r"(?i)^/?users/([^/]+)/")

_MAILBOX_ID_CACHE: dict = {}


def _resource_user_segment(resource: str) -> str:
    """The mailbox identifier out of a notification `resource`, lowercased."""
    m = _RESOURCE_USER_RE.match((resource or '').strip())
    return (m.group(1) or '').strip().lower() if m else ''


def _mailbox_identifiers(graph: GraphClient, mailbox: str) -> set:
    """Every identifier Graph may use for the sanctioned mailbox: its UPN
    and its directory object id. The object id is resolved once and cached
    for the life of the process."""
    upn = (mailbox or '').strip().lower()
    ids = {upn} if upn else set()
    if upn and upn not in _MAILBOX_ID_CACHE:
        obj_id = ''
        try:
            resp = graph._request('GET', f'/users/{mailbox}?$select=id')
            if resp.status_code < 400:
                obj_id = (resp.json().get('id') or '').strip().lower()
            else:
                log.warning('could not resolve object id for %s: HTTP %s',
                            mailbox, resp.status_code)
        except Exception:
            log.exception('could not resolve object id for %s', mailbox)
        _MAILBOX_ID_CACHE[upn] = obj_id
    obj_id = _MAILBOX_ID_CACHE.get(upn)
    if obj_id:
        ids.add(obj_id)
    return ids


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
    # Use service.crm_inbox_email() so the fallback (leads@procamgroup.in)
    # applies even if the env var is unset — matches how the subscription
    # was created.
    from . import service as _mail
    mailbox = _mail.crm_inbox_email()
    if not mailbox:
        log.error('CRM_INBOX_EMAIL not set — cannot process webhook.')
        return {'error': 'not configured'}

    notifications = payload.get('value', []) if isinstance(payload, dict) else []
    if not notifications:
        return {'processed': 0, 'note': 'empty payload'}

    graph = GraphClient()
    stats = {'processed': 0, 'created': 0, 'skipped': 0, 'failed': 0,
             'rejected_mailbox': 0}

    with app.app_context():
        for n in notifications:
            import hmac as _hmac
            if secret_expected and not _hmac.compare_digest(
                    (n.get('clientState') or ''), secret_expected):
                log.warning('webhook: clientState mismatch — rejecting one item')
                stats['failed'] += 1
                _log_event(db, EmailEvent, None, mailbox, 'rejected',
                           reason='clientState mismatch', payload=n)
                continue

            resource = n.get('resource') or ''

            # v2026-09-02 — Mailbox lockdown. Every notification must be for
            # the sanctioned leads mailbox, so a rogue or accidental extra
            # subscription can never seed a Lead.
            #
            # The cheap check compares the resource's user segment against
            # the mailbox's UPN *and* its directory object id, because Graph
            # uses either (see _RESOURCE_USER_RE above). A mismatch is NOT
            # a rejection on its own — an earlier version matched only the
            # UPN as a substring and silently rejected every real
            # notification. Instead we fall through to the fetch, which is
            # scoped to the sanctioned mailbox: Graph 404s a message id
            # belonging to any other mailbox, and that is the real
            # guarantee. Cheap check first, authoritative check second.
            seg = _resource_user_segment(resource)
            resource_matches = bool(seg and seg in _mailbox_identifiers(graph, mailbox))
            if not resource_matches:
                log.warning('webhook: resource %r does not name %s — '
                            'verifying with a scoped fetch',
                            resource[:160], mailbox)

            msg_ref  = n.get('resourceData', {}).get('id')
            if not msg_ref:
                log.warning('webhook: no resourceData.id in notification')
                stats['failed'] += 1
                _log_event(db, EmailEvent, None, mailbox, 'failed',
                           reason='no message id', payload=n)
                continue

            evt = _log_event(db, EmailEvent, None, mailbox, 'received',
                             reason=None, payload=n)

            # Fetch + process. The fetch is scoped to the sanctioned
            # mailbox, so success here proves the message really is in it.
            try:
                msg = _get_message(graph, mailbox, msg_ref)
            except Exception as e:
                if not resource_matches:
                    # Didn't name our mailbox AND isn't in it — genuinely
                    # someone else's message. This is the lockdown firing.
                    log.warning('webhook: rejecting notification for a '
                                'different mailbox — resource=%r', resource[:160])
                    evt.status = 'rejected'
                    evt.reason = f'wrong mailbox: {resource[:120]}'
                    stats['rejected_mailbox'] += 1
                else:
                    log.exception('webhook: fetch %s failed', msg_ref)
                    evt.status = 'failed'
                    evt.reason = f'fetch: {e}'
                    stats['failed'] += 1
                db.session.commit()
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
