"""
v2026-08 — Microsoft Graph subscription helpers.

Graph push notifications require an app-created "subscription" that ties
a mailbox to a webhook URL. The subscription expires after ~3 days (max
for messages), so we also expose a renew() helper you can wire to a
cron / heartbeat.

Env vars required:
    CRM_INBOX_EMAIL      — the mailbox
    EMAIL_WEBHOOK_URL    — public HTTPS URL of /api/email/webhook
    EMAIL_WEBHOOK_SECRET — random string; Graph echoes it back on every
                           notification so we can verify authenticity
"""
from __future__ import annotations
import os
import logging
from datetime import datetime, timedelta, timezone

from .graph_client import GraphClient

log = logging.getLogger(__name__)

# Graph's maximum for message-change subscriptions is 4230 minutes (~70.5 h).
# We renew every ~48 h and set expiry ~68 h out for safety margin.
_EXPIRE_MINUTES = 4230


def _expiry_iso() -> str:
    dt = datetime.now(timezone.utc) + timedelta(minutes=_EXPIRE_MINUTES)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.000Z')


def _need(env: str) -> str:
    v = os.environ.get(env)
    if not v:
        raise RuntimeError(f'{env} env var is required for mailbox mode.')
    return v


def _json_or_raise(resp, context: str) -> dict:
    """GraphClient._request returns a raw requests.Response. Parse it,
    raise a clear error on non-2xx."""
    if resp.status_code >= 400:
        raise RuntimeError(
            f'Graph {context} failed: HTTP {resp.status_code} — '
            f'{resp.text[:500]}'
        )
    try:
        return resp.json()
    except Exception:
        return {}


def create() -> dict:
    """Create a new subscription. Returns the Graph subscription resource."""
    mailbox      = _need('CRM_INBOX_EMAIL')
    webhook_url  = _need('EMAIL_WEBHOOK_URL')
    client_state = _need('EMAIL_WEBHOOK_SECRET')

    graph = GraphClient()
    payload = {
        'changeType':          'created',
        'notificationUrl':     webhook_url,
        'resource':            f"/users/{mailbox}/mailFolders('Inbox')/messages",
        'expirationDateTime':  _expiry_iso(),
        'clientState':         client_state,
    }
    resp = graph._request('POST', '/subscriptions', json_body=payload)
    res  = _json_or_raise(resp, 'create subscription')
    log.info('Graph subscription created: id=%s expires=%s',
             res.get('id'), res.get('expirationDateTime'))
    return res


def renew(subscription_id: str) -> dict:
    graph = GraphClient()
    payload = {'expirationDateTime': _expiry_iso()}
    resp = graph._request('PATCH', f'/subscriptions/{subscription_id}',
                          json_body=payload)
    res  = _json_or_raise(resp, 'renew subscription')
    log.info('Graph subscription renewed: id=%s new expiry=%s',
             subscription_id, res.get('expirationDateTime'))
    return res


def delete(subscription_id: str) -> None:
    graph = GraphClient()
    resp = graph._request('DELETE', f'/subscriptions/{subscription_id}')
    if resp.status_code >= 400 and resp.status_code != 404:
        raise RuntimeError(
            f'Graph delete subscription failed: HTTP {resp.status_code} — '
            f'{resp.text[:500]}'
        )
    log.info('Graph subscription deleted: %s', subscription_id)


def list_active() -> list:
    graph = GraphClient()
    resp = graph._request('GET', '/subscriptions')
    data = _json_or_raise(resp, 'list subscriptions')
    return data.get('value', [])
