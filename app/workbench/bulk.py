"""
Bulk updates from the Workbench — preview first, then apply.

The safety model is Data Quality's, deliberately: every change is shown
before it happens, the preview is signed, and applying re-plans from the
database and refuses if what would change is no longer what was shown.
Where an action already exists in `app.bulk_admin.service` this calls
it rather than writing a second path.

What it adds over bulk_admin: per-record results. A batch of forty where
two fail reports those two by name and still saves the other thirty-eight.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import date, datetime, timedelta

from flask import current_app, g

from app.services import sales_rules as rules

#: Records per batch. Larger selections are sent in several requests so
#: the page can show progress.
BATCH_MAX = 200
#: How long a preview stays valid.
PREVIEW_TTL_SECONDS = 600
#: A date set in bulk may not be further ahead than this.
MAX_FUTURE_DAYS = 366

#: action → (label, value kind, what it writes)
ACTIONS = {
    'set_followup': ('Set the next follow-up date', 'date', 'followup_date'),
    'set_next_action': ('Set the next action', 'text', 'next_action'),
    'complete_followup': ('Mark the follow-up done', 'date', 'followup_date'),
    'assign_owner': ('Assign an owner', 'employee', 'assigned_to'),
    'set_stage': ('Change the stage', 'stage', 'stage'),
    'set_vertical': ('Set the vertical', 'vertical', 'procam_vertical'),
    'log_activity': ('Log the same activity', 'text', 'activity'),
    'archive': ('Archive (reversible)', None, 'is_archived'),
}


class BulkRefused(Exception):
    """Refusal the caller turns straight into a JSON error."""

    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.message, self.status, self.extra = message, status, extra


def _ids(raw):
    if not isinstance(raw, list) or not raw:
        raise BulkRefused('Choose at least one record')
    out = []
    for v in raw:
        if isinstance(v, bool) or not str(v).strip().isdigit():
            raise BulkRefused('Record ids must be numbers')
        if int(v) not in out:
            out.append(int(v))
    if len(out) > BATCH_MAX:
        raise BulkRefused(f'At most {BATCH_MAX} records per request', 413)
    return out


def _value(action, raw):
    """(what gets stored, how it reads back) — validated per kind."""
    kind = ACTIONS[action][1]
    if kind is None:
        return True, ACTIONS[action][0]
    text = '' if raw is None else str(raw).strip()
    if kind == 'date':
        if action == 'complete_followup':
            return None, 'cleared'
        try:
            when = datetime.strptime(text, '%Y-%m-%d').date()
        except ValueError:
            raise BulkRefused('Give the date as YYYY-MM-DD')
        if when < rules.business_today():
            raise BulkRefused('That date has already passed')
        if when > rules.business_today() + timedelta(days=MAX_FUTURE_DAYS):
            raise BulkRefused('That date is too far ahead')
        return when, str(when)
    if kind == 'text':
        if not text:
            raise BulkRefused('Type what the next action is')
        return text[:200], text[:200]
    if kind == 'employee':
        from app import Employee
        emp = Employee.query.filter_by(emp_code=text).first()
        if emp is None or emp.is_active is False:
            raise BulkRefused(f'{text or "That employee"} is not an active employee')
        return emp.emp_code, emp.name or emp.emp_code
    if kind == 'stage':
        from app import STAGES_ALL
        if text not in STAGES_ALL:
            raise BulkRefused(f'Unknown stage {text!r}')
        return text, text
    if kind == 'vertical':
        from app.master_data import service as md
        labels = [i.label for i in md.items('vertical')]
        if text not in labels:
            raise BulkRefused(f'{text!r} is not a vertical in Master Data')
        return text, text
    raise BulkRefused('Unknown action')


def _scoped_leads(ids, sc):
    from app import Lead
    from app.access import scope as sc_mod
    rows = sc_mod.leads(Lead.query.filter(Lead.id.in_(ids)), sc=sc).all()
    return {l.id: l for l in rows}


def _before(lead, action):
    field = ACTIONS[action][2]
    if field == 'activity':
        return ''
    v = getattr(lead, field, None)
    return str(v) if v not in (None, '') else ''


def plan(ids, action, value, sc):
    """(changes, skipped, stored value). Raises BulkRefused."""
    if action not in ACTIONS:
        raise BulkRefused('Unknown action', 404)
    ids = _ids(ids)
    stored, label = _value(action, value)
    visible = _scoped_leads(ids, sc)
    missing = [i for i in ids if i not in visible]
    if missing:
        # Same answer whether the record does not exist or is somebody
        # else's: the ids must not become a way of probing.
        raise BulkRefused('Some of those records are not yours to change',
                          403, refused=missing)
    changes, skipped = [], []
    for lid in ids:
        lead = visible[lid]
        before = _before(lead, action)
        if action == 'set_stage' and lead.stage == stored:
            skipped.append({'id': lid, 'name': lead.company,
                            'reason': 'already at this stage'})
            continue
        if action == 'archive' and lead.is_archived:
            skipped.append({'id': lid, 'name': lead.company,
                            'reason': 'already archived'})
            continue
        if action not in ('log_activity', 'complete_followup') \
                and before == (str(stored) if stored is not None else ''):
            skipped.append({'id': lid, 'name': lead.company,
                            'reason': 'already set to this value'})
            continue
        if action == 'complete_followup' and not lead.followup_date:
            skipped.append({'id': lid, 'name': lead.company,
                            'reason': 'no follow-up was due'})
            continue
        changes.append({'id': lid, 'name': lead.company,
                        'field': ACTIONS[action][2], 'before': before,
                        'after': '' if stored is None else str(stored),
                        'after_label': label})
    return changes, skipped, stored


def _secret():
    key = current_app.secret_key
    if not key:
        raise BulkRefused('The server has no signing key configured', 500)
    return key if isinstance(key, bytes) else str(key).encode()


def sign(action, value, actor, changes, expires):
    payload = json.dumps({
        'action': action, 'value': str(value), 'actor': actor or '',
        'expires': expires,
        'changes': [[c['id'], c['field'], c['before'], c['after']]
                    for c in changes],
    }, sort_keys=True, separators=(',', ':')).encode()
    return hmac.new(_secret(), payload, hashlib.sha256).hexdigest()


def preview(ids, action, value, *, sc, actor, now=None):
    changes, skipped, stored = plan(ids, action, value, sc)
    now = int((now or datetime.utcnow()).timestamp())
    expires = now + PREVIEW_TTL_SECONDS
    return {
        'action': action, 'action_label': ACTIONS[action][0],
        'value': '' if stored is None else str(stored),
        'changes': changes, 'skipped': skipped, 'count': len(changes),
        'expires_at': expires,
        'preview_token': (f'{expires}.{sign(action, stored, actor, changes, expires)}'
                          if changes else None),
    }


def apply(ids, action, value, reason, token, *, sc, actor, now=None):
    """Apply a previewed batch. Returns per-record results."""
    reason = (reason or '').strip()
    if len(reason) < 5:
        raise BulkRefused('Give a reason — it is recorded against every record')
    try:
        expires_s, digest = str(token or '').split('.', 1)
        expires = int(expires_s)
    except (ValueError, AttributeError):
        raise BulkRefused('Preview again before applying')
    now_i = int((now or datetime.utcnow()).timestamp())
    if expires < now_i:
        raise BulkRefused('That preview has expired — preview again', 409)
    if expires > now_i + PREVIEW_TTL_SECONDS:
        raise BulkRefused('Preview again before applying', 409)

    changes, skipped, stored = plan(ids, action, value, sc)
    if not hmac.compare_digest(sign(action, stored, actor, changes, expires),
                               digest):
        raise BulkRefused('The records changed since the preview — '
                          'preview again', 409)
    return _write(action, stored, reason, actor, changes, skipped)


def _write(action, stored, reason, actor, changes, skipped):
    from app import db, Lead, LeadActivity
    from app.services import audit

    batch_ref = uuid.uuid4().hex[:12]
    applied, failed = [], []
    previous_reason = getattr(g, 'audit_reason', None)
    g.audit_reason = reason
    try:
        audit.record('workbench.bulk', 'lead_batch', batch_ref, strict=True,
                     new={'action': action, 'value': str(stored),
                          'count': len(changes),
                          'ids': [c['id'] for c in changes][:BATCH_MAX]},
                     reason=reason)
        for change in changes:
            try:
                lead = db.session.get(Lead, change['id'])
                if lead is None:
                    failed.append({**change, 'error': 'no longer exists'})
                    continue
                with db.session.begin_nested():
                    if action == 'assign_owner':
                        from app.services import lead_assignment
                        ok, err = lead_assignment.assign(
                            lead, primary_code=stored, actor=actor,
                            note=reason, _defer_commit=True)
                        if not ok:
                            raise ValueError(err)
                    elif action == 'log_activity':
                        db.session.add(LeadActivity(
                            lead_id=lead.id, kind='note', subject=stored[:255],
                            body=reason, performed_by=actor,
                            occurred_at=datetime.utcnow()))
                    elif action == 'archive':
                        lead.is_archived = True
                        lead.archived_at = datetime.utcnow()
                        lead.archived_by = actor
                        lead.archive_reason = reason[:200]
                    else:
                        setattr(lead, ACTIONS[action][2], stored)
                    lead.updated_at = datetime.utcnow()
                applied.append(change)
            except Exception as exc:                  # one record only
                current_app.logger.exception(
                    'workbench bulk %s failed for lead %s', action, change['id'])
                failed.append({**change, 'error': str(exc)[:160] or 'could not be saved'})
        db.session.commit()
    finally:
        g.audit_reason = previous_reason
    return {'applied': applied, 'failed': failed, 'skipped': skipped,
            'applied_count': len(applied), 'failed_count': len(failed),
            'batch_ref': batch_ref}
