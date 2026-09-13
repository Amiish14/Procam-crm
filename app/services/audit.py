"""The audit trail: one call for every business action worth recording.

    from app.services import audit
    audit.record('employee.update', 'employee', emp.emp_code,
                 old={'role': 'user'}, new={'role': 'admin'})

    before = audit.snapshot(lead, ('stage', 'assigned_to'))
    ... change the lead ...
    audit.record_change('lead.update', 'lead', lead.id, before,
                        audit.snapshot(lead, ('stage', 'assigned_to')))

What is recorded: time, the signed-in user and their role, the action,
the entity, only the fields that changed (old and new), the reason when
there is one, the client IP and user agent.

What is never recorded: a password, token, secret or key. Any field whose
name looks like one is stored as "[redacted]", whatever the caller passes.

Failure policy: an audit write must not break the business action it
describes, so errors are logged and swallowed — except where the caller
passes ``strict=True`` (used before destructive actions, where no record
means no action).
"""
import logging
import re
from datetime import date, datetime
from decimal import Decimal

log = logging.getLogger(__name__)

REDACTED = '[redacted]'
_SECRET = re.compile(r'pass(word)?|secret|token|api[_-]?key|hash|credential',
                     re.I)
#: Longest text kept for one value; notes and email bodies are evidence
#: elsewhere, the trail only needs to show that they changed.
MAX_TEXT = 500


def _clean(value, key=''):
    if key and _SECRET.search(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(k): _clean(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_clean(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str) and len(value) > MAX_TEXT:
        return value[:MAX_TEXT] + '…'
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def snapshot(obj, fields):
    """The named attributes of a record, as plain values."""
    return {f: _clean(getattr(obj, f, None), f) for f in fields}


def changes(before, after):
    """(old, new) holding only the keys whose values differ."""
    before, after = before or {}, after or {}
    keys = [k for k in dict.fromkeys(list(before) + list(after))
            if before.get(k) != after.get(k)]
    return ({k: before.get(k) for k in keys}, {k: after.get(k) for k in keys})


def _request_context():
    try:
        from flask import has_request_context, request, session
    except ImportError:                                # pragma: no cover
        return None, None, None, None
    if not has_request_context():
        return None, None, None, None
    ip = (request.remote_addr or '')[:64] or None
    ua = (request.headers.get('User-Agent') or '')[:200] or None
    return session.get('emp_code'), session.get('role'), ip, ua


def record(action, entity_type, entity_id=None, *, old=None, new=None,
           reason=None, actor=None, commit=False, strict=False,
           session=None):
    """Add one audit event to the session. Returns it, or None on failure.

    Without ``commit`` the event rides the caller's transaction, which is
    what most callers want: the change and its record land together.
    """
    from app import db
    from app.models.audit import AuditEvent
    try:
        who, role, ip, ua = _request_context()
        event = AuditEvent(
            actor=(actor or who or 'system')[:20], actor_role=role,
            action=action[:60], entity_type=entity_type[:40],
            entity_id=None if entity_id is None else str(entity_id)[:60],
            old_value=_clean(old) if old is not None else None,
            new_value=_clean(new) if new is not None else None,
            reason=(reason or '')[:400] or None, ip=ip, user_agent=ua)
        target = session if session is not None else db.session
        target.add(event)
        if commit:
            target.commit()
        return event
    except Exception:
        log.exception('audit write failed: %s %s %s', action, entity_type,
                      entity_id)
        if strict:
            raise
        return None


def record_change(action, entity_type, entity_id, before, after, **kw):
    """record() with only what changed. Nothing changed, nothing written."""
    old, new = changes(before, after)
    if not old and not new:
        return None
    return record(action, entity_type, entity_id, old=old, new=new, **kw)
