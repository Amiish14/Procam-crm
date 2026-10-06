"""
The seam between the CRM and the TMS.

Two applications, two databases, no shared code and no cross-database
reads. They talk over an authenticated HTTP API, and this is the CRM's
half of it: who may call, what makes a call safe to retry, and what
gets written down about it.

**Authentication** is a bearer token per calling system, configured as
`CRM_INTEGRATION_TOKENS` — `name:token` pairs, comma separated, read
from the environment and never from source. Compared with
`compare_digest`, because comparing secrets with `==` leaks their
length and prefix to anyone willing to time it.

**Idempotency** is the caller's own key. The same key seen twice
returns the first result rather than doing the work again, which is
what makes a network timeout safe: the TMS cannot know whether a
request that never answered was applied, so it must be free to ask
again.

**Logging** is every request, in both directions, with the identifiers
on both sides. A failure between two systems that each assume the
other has it is otherwise a silence.

What this deliberately does not do: invent business behaviour. It
looks things up, creates what the TMS asks for, and records the link.
What a TMS project *means* is the TMS's business.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import time
import uuid

log = logging.getLogger(__name__)

#: How long a remembered idempotency key is honoured. Long enough to
#: cover a retry storm and a queue drain; short enough that the table
#: does not become a permanent index of every request ever made.
IDEMPOTENCY_WINDOW_HOURS = 48

API_PREFIX = '/api/integration/v1'


class IntegrationError(Exception):
    """A refusal the route turns into a structured error response."""

    def __init__(self, message, status=400, code='bad_request'):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


# ── who may call ─────────────────────────────────────────────────────
def _tokens():
    """{caller name: token} from the environment. Never from source."""
    raw = os.environ.get('CRM_INTEGRATION_TOKENS') or ''
    out = {}
    for part in raw.split(','):
        part = part.strip()
        if not part or ':' not in part:
            continue
        name, _, token = part.partition(':')
        name, token = name.strip(), token.strip()
        if name and token:
            out[name] = token
    return out


def authenticate(header_value):
    """The caller's name, or None.

    `Authorization: Bearer <token>`. The comparison is constant-time
    and runs against every configured token rather than stopping at
    the first match, so the time it takes does not say which system
    the token belongs to.
    """
    raw = (header_value or '').strip()
    if raw.lower().startswith('bearer '):
        raw = raw[7:].strip()
    if not raw:
        return None
    found = None
    for name, token in _tokens().items():
        if hmac.compare_digest(raw, token):
            found = name
    return found


def configured():
    """Is server-to-server access switched on at all?"""
    return bool(_tokens())


# ── idempotency ──────────────────────────────────────────────────────
def remembered(idempotency_key, endpoint):
    """The result of an identical earlier call, if there was one."""
    from datetime import datetime, timedelta

    from app.models.integration import IntegrationLog

    key = (idempotency_key or '').strip()
    if not key:
        return None
    since = datetime.utcnow() - timedelta(hours=IDEMPOTENCY_WINDOW_HOURS)
    row = (IntegrationLog.query
           .filter(IntegrationLog.idempotency_key == key,
                   IntegrationLog.endpoint == endpoint,
                   IntegrationLog.status == 'ok',
                   IntegrationLog.created_at >= since)
           .order_by(IntegrationLog.id.desc()).first())
    if row is None:
        return None
    try:
        return json.loads(row.response_summary or 'null')
    except Exception:
        return None


# ── the log ──────────────────────────────────────────────────────────
def record(*, direction, endpoint, method='POST', status='ok',
           status_code=200, request_id=None, idempotency_key=None,
           crm_object_type=None, crm_object_id=None, tms_object_id=None,
           request_summary=None, response_summary=None, error=None,
           caller=None, started=None, commit=True):
    """One row per request. Never raises."""
    from app import db
    from app.models.integration import IntegrationLog

    try:
        row = IntegrationLog(
            request_id=(request_id or '')[:80] or None,
            idempotency_key=(idempotency_key or '')[:200] or None,
            direction=direction, endpoint=(endpoint or '')[:200],
            method=method, status=status, status_code=status_code,
            crm_object_type=crm_object_type,
            crm_object_id=(str(crm_object_id)[:60]
                           if crm_object_id is not None else None),
            tms_object_id=(str(tms_object_id)[:60]
                           if tms_object_id is not None else None),
            request_summary=_trim(request_summary),
            response_summary=_trim(response_summary),
            error=(error or '')[:500] or None,
            caller=(caller or '')[:80] or None,
            duration_ms=(int((time.monotonic() - started) * 1000)
                         if started else None))
        db.session.add(row)
        if commit:
            db.session.commit()
        return row
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        log.exception('could not write the integration log')
        return None


def _trim(value, limit=4000):
    """JSON, trimmed, with the obvious secrets taken out.

    An integration log that records an Authorization header is a
    second place the token lives.
    """
    if value is None:
        return None
    try:
        if isinstance(value, (dict, list)):
            scrubbed = _scrub(value)
            text = json.dumps(scrubbed, default=str)
        else:
            text = str(value)
    except Exception:
        text = str(value)
    return text[:limit]


_SECRET_KEYS = ('authorization', 'token', 'password', 'secret', 'api_key',
                'apikey')


def _scrub(value):
    if isinstance(value, dict):
        return {k: ('(hidden)' if k.lower() in _SECRET_KEYS else _scrub(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def new_request_id():
    return uuid.uuid4().hex


# ── the link ─────────────────────────────────────────────────────────
def link(*, crm_lead_id=None, crm_account_id=None, tms_project_id=None,
         tms_job_id=None, link_type='project', created_by=None, note=None):
    """Record which CRM record is which TMS record.

    Idempotent: the same pairing twice returns the existing row. The
    unique index is what guarantees that, not this query — two
    requests arriving together would both pass a check and only one
    can pass the index.
    """
    from app import db
    from app.models.integration import CrmTmsLink

    if not (crm_lead_id or crm_account_id):
        raise IntegrationError('Name a CRM record to link',
                               code='missing_crm_id')
    if not (tms_project_id or tms_job_id):
        raise IntegrationError('Name a TMS record to link to',
                               code='missing_tms_id')

    existing = CrmTmsLink.query.filter_by(
        crm_lead_id=crm_lead_id, tms_project_id=tms_project_id,
        link_type=link_type).first()
    if existing is not None:
        if tms_job_id and not existing.tms_job_id:
            existing.tms_job_id = tms_job_id
            db.session.commit()
        return existing, False

    row = CrmTmsLink(crm_lead_id=crm_lead_id, crm_account_id=crm_account_id,
                     tms_project_id=tms_project_id, tms_job_id=tms_job_id,
                     link_type=link_type, created_by=created_by,
                     note=(note or '')[:400] or None)
    db.session.add(row)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        again = CrmTmsLink.query.filter_by(
            crm_lead_id=crm_lead_id, tms_project_id=tms_project_id,
            link_type=link_type).first()
        if again is not None:
            return again, False
        raise
    return row, True


def links_for_lead(lead_id):
    from app.models.integration import CrmTmsLink
    return CrmTmsLink.query.filter_by(crm_lead_id=lead_id).all()


# ── CRM → TMS ────────────────────────────────────────────────────────
def notify_tms(event, payload, *, crm_object_type=None, crm_object_id=None,
               timeout=None, event_id=None):
    """Tell the TMS something happened — durably.

    This used to POST inline and log the failure, which was wrong for
    these two events in particular. The TMS cannot discover a
    `lead.won` it never received: a deal that was won and never
    announced looks identical, from the outside, to a deal that was
    always won. "Poll as well as listen" was advice that does not
    work, and the TMS was right to say so.

    So the event is written to `webhook_outbox` inside the caller's
    transaction and `scripts/webhook_worker.py` delivers it, retrying
    until the TMS takes it or the attempt ceiling is reached. The
    caller waits for no network call and a win is never rolled back
    because another system was down.

    Returns True when the event is now owed to the TMS — queued, or
    already queued by an earlier call. False only when nothing was
    recorded.
    """
    from app.services import webhooks

    row = webhooks.enqueue(event, payload, crm_object_type=crm_object_type,
                           crm_object_id=crm_object_id, event_id=event_id)
    if row is not None:
        return True

    # Already queued. The unique index on event_id refused a second
    # copy, which is the behaviour we want — the TMS is still owed the
    # event exactly once.
    from app.models.integration import WebhookOutbox
    key = event_id or webhooks._event_id(event, crm_object_id)
    return WebhookOutbox.query.filter_by(event_id=key).first() is not None


def _now_iso():
    from datetime import datetime
    return datetime.utcnow().isoformat(timespec='seconds') + 'Z'
