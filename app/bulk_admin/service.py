"""Bulk lead administration — §52-58.

The shape of this module is set by §54 and §56: archiving is the normal
action, permanent deletion is the exception, and deletion is BLOCKED where
material commercial history exists rather than merely warned about.

"Do not destroy commercial history casually."
"""
import uuid
from datetime import datetime

from app import db


# Links that make a lead part of the commercial record.  Their presence
# blocks permanent deletion outright (§56) — an opportunity or a quote is
# history someone will need to answer for later.
MATERIAL = ('opportunities', 'rfqs', 'quotes')
# Worth showing before deleting, but not on their own a reason to refuse.
INCIDENTAL = ('activities', 'attachments', 'tasks', 'stage_history')


def _model(path):
    import importlib
    module_path, _, attr = path.partition(':')
    try:
        return getattr(importlib.import_module(module_path), attr)
    except Exception:
        return None


def linked_counts(lead_ids):
    """§56 — everything that hangs off these leads, before anything is done."""
    from app import Lead, LeadActivity, LeadAttachment, Opportunity
    if not lead_ids:
        return {}

    counts = {
        'activities': LeadActivity.query.filter(
            LeadActivity.lead_id.in_(lead_ids)).count(),
        'attachments': LeadAttachment.query.filter(
            LeadAttachment.lead_id.in_(lead_ids)).count(),
        'opportunities': Opportunity.query.filter(
            Opportunity.lead_id.in_(lead_ids)).count(),
    }

    history = _model('app:LeadStageHistory')
    if history is not None:
        counts['stage_history'] = history.query.filter(
            history.lead_id.in_(lead_ids)).count()

    for key, path, column in (('rfqs', 'app.models.rfq:RFQ', 'lead_id'),
                              ('quotes', 'app.models.quote:Quote', 'lead_id')):
        model = _model(path)
        if model is not None:
            try:
                counts[key] = model.query.filter(
                    getattr(model, column).in_(lead_ids)).count()
            except Exception:
                counts[key] = 0

    tasks = _model('app.models.task_engine:TaskInstance')
    if tasks is not None:
        counts['tasks'] = tasks.query.filter(
            tasks.entity_type == 'Lead',
            tasks.entity_id.in_(lead_ids)).count()

    won = Opportunity.query.filter(Opportunity.lead_id.in_(lead_ids),
                                   Opportunity.stage == 'Won').count()
    counts['won_deals'] = won
    return counts


def deletion_blocked(counts):
    """Why permanent deletion is refused, or an empty list if it is not."""
    reasons = []
    if counts.get('won_deals'):
        reasons.append(
            f'{counts["won_deals"]} Won deal(s) are linked — deleting these '
            f'would remove business Procam has actually done')
    for key in MATERIAL:
        n = counts.get(key) or 0
        if n and key != 'opportunities':
            reasons.append(f'{n} {key} linked')
        elif n and key == 'opportunities':
            reasons.append(f'{n} opportunit{"y" if n == 1 else "ies"} linked')
    return reasons


def _snapshot(lead):
    return {
        'id': lead.id, 'company': lead.company, 'project': lead.project,
        'stage': lead.stage, 'assigned_to': lead.assigned_to,
        'source': lead.source, 'company_id': lead.company_id,
        'created_at': str(lead.created_at) if lead.created_at else '',
    }


def _audit(lead, action, reason, actor, counts, batch_ref):
    from app.models.audit import DeletionAudit
    db.session.add(DeletionAudit(
        entity_type='Lead', entity_id=lead.id, action=action,
        reason=(reason or '')[:400], snapshot=_snapshot(lead),
        linked=counts, performed_by=actor or '', batch_ref=batch_ref))


# ── §53 bulk actions ─────────────────────────────────────────────────
def archive(lead_ids, reason, actor):
    """§54 — the normal way to remove leads from the active pipeline."""
    from app import Lead
    batch_ref = uuid.uuid4().hex[:12]
    rows = Lead.query.filter(Lead.id.in_(lead_ids),
                             Lead.is_archived.isnot(True)).all()
    counts = linked_counts([l.id for l in rows])
    now = datetime.utcnow()
    for lead in rows:
        _audit(lead, 'archive', reason, actor, {}, batch_ref)
        lead.is_archived = True
        lead.archived_at = now
        lead.archived_by = actor or ''
        lead.archive_reason = (reason or '')[:200]
    db.session.commit()
    return {'archived': len(rows), 'batch_ref': batch_ref, 'linked': counts}


def unarchive(lead_ids, actor):
    from app import Lead
    rows = Lead.query.filter(Lead.id.in_(lead_ids),
                             Lead.is_archived.is_(True)).all()
    for lead in rows:
        _audit(lead, 'restore', 'restored to the active pipeline', actor,
               {}, '')
        lead.is_archived = False
        lead.archived_at = None
        lead.archived_by = None
        lead.archive_reason = None
    db.session.commit()
    return {'restored': len(rows)}


def assign(lead_ids, emp_code, actor):
    from app import Lead, Employee
    employee = Employee.query.filter_by(emp_code=emp_code).first()
    if employee is None:
        raise ValueError(f'{emp_code} is not an employee code')
    if not employee.is_active:
        raise ValueError(f'{employee.name} is not an active employee')

    rows = Lead.query.filter(Lead.id.in_(lead_ids)).all()
    for lead in rows:
        lead.assigned_to = employee.emp_code
        lead.assigned_name = employee.name
    db.session.commit()
    return {'assigned': len(rows), 'to': employee.name}


def set_field(lead_ids, field, value, actor):
    """Change vertical or priority in bulk, validated against Master Data."""
    from app import Lead
    from app.master_data import service as md

    allowed_field = {'procam_vertical': 'vertical', 'stage': None}
    if field not in allowed_field:
        raise ValueError(f'{field} cannot be set in bulk')

    lookup = allowed_field[field]
    if lookup:
        allowed = {i.label for i in md.items(lookup)}
        if value not in allowed:
            raise ValueError(
                f'"{value}" is not a known {lookup} — add it under Master '
                f'Data first')

    rows = Lead.query.filter(Lead.id.in_(lead_ids)).all()
    for lead in rows:
        setattr(lead, field, value)
    db.session.commit()
    return {'updated': len(rows), 'field': field, 'value': value}


def delete(lead_ids, reason, actor):
    """§55-57 — permanent, restricted, audited, and refused where history
    would be destroyed.

    The audit rows are written and committed BEFORE the deletion, so the
    record of what was removed survives even if the delete itself fails
    part-way.
    """
    from app import Lead
    if not (reason or '').strip():
        raise ValueError('A reason is required for permanent deletion')

    rows = Lead.query.filter(Lead.id.in_(lead_ids)).all()
    if not rows:
        return {'deleted': 0}

    counts = linked_counts([l.id for l in rows])
    blocked = deletion_blocked(counts)
    if blocked:
        raise PermissionError(
            'Permanent deletion refused — ' + '; '.join(blocked)
            + '. Archive these instead: the records leave the active '
              'pipeline but the history stays.')

    batch_ref = uuid.uuid4().hex[:12]
    for lead in rows:
        _audit(lead, 'delete', reason, actor, counts, batch_ref)
    db.session.commit()          # the audit survives whatever follows

    from app import LeadActivity, LeadAttachment
    ids = [l.id for l in rows]
    LeadActivity.query.filter(LeadActivity.lead_id.in_(ids)).delete(
        synchronize_session=False)
    LeadAttachment.query.filter(LeadAttachment.lead_id.in_(ids)).delete(
        synchronize_session=False)
    history = _model('app:LeadStageHistory')
    if history is not None:
        history.query.filter(history.lead_id.in_(ids)).delete(
            synchronize_session=False)
    Lead.query.filter(Lead.id.in_(ids)).delete(synchronize_session=False)
    db.session.commit()
    return {'deleted': len(rows), 'batch_ref': batch_ref}
