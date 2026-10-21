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


def close_blocked(counts):
    """Why a bulk close as Lost is refused.

    Narrower than `deletion_blocked`, and deliberately so. That one
    refused where a quote, an RFQ or an opportunity was attached,
    because destroying the lead would have orphaned them. Closing
    destroys nothing, and a lead going Lost after it was quoted is
    the commonest outcome in the book — refusing it would make the
    bulk tool useless for the one thing it is now for.

    A Won deal is different. Marking a lead Lost when Procam did
    the work says something false about business that happened, and
    no amount of bulk convenience justifies it.
    """
    if counts.get('won_deals'):
        return [f'{counts["won_deals"]} Won deal(s) are linked — '
                f'closing these as Lost would say Procam did not '
                f'win work it did']
    return []


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
    _bulk_event('bulk.archive', rows, actor, reason, batch_ref)
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
    _bulk_event('bulk.restore', rows, actor, None)
    for lead in rows:
        _audit(lead, 'restore', 'restored to the active pipeline', actor,
               {}, '')
        lead.is_archived = False
        lead.archived_at = None
        lead.archived_by = None
        lead.archive_reason = None
    db.session.commit()
    return {'restored': len(rows)}


def _bulk_event(action, rows, actor, reason, batch_ref=None, extra=None):
    """One summary event per bulk operation, naming every lead touched.
    The per-lead field changes are written by the session listener."""
    from app.services import audit as audit_trail
    ids = [l.id for l in rows]
    audit_trail.record(action, 'lead_batch', batch_ref or None, actor=actor,
                       new={'count': len(ids), 'lead_ids': ids[:2000],
                            **(extra or {})},
                       reason=reason)


def assign(lead_ids, emp_code, actor):
    from app import Lead, Employee
    employee = Employee.query.filter_by(emp_code=emp_code).first()
    if employee is None:
        raise ValueError(f'{emp_code} is not an employee code')
    if not employee.is_active:
        raise ValueError(f'{employee.name} is not an active employee')

    from app import LeadAssignmentHistory

    rows = Lead.query.filter(Lead.id.in_(lead_ids)).all()
    _bulk_event('bulk.assign', rows, actor, None,
                extra={'to': employee.emp_code})
    moved = []
    for lead in rows:
        if lead.assigned_to != employee.emp_code:
            db.session.add(LeadAssignmentHistory(
                lead_id=lead.id, from_primary=lead.assigned_to or '',
                to_primary=employee.emp_code,
                from_secondary=lead.secondary_owner or '',
                to_secondary=lead.secondary_owner or '',
                changed_at=datetime.utcnow(), changed_by=actor,
                note='bulk admin reassignment'))
            moved.append(lead.id)
        lead.assigned_to = employee.emp_code
        lead.assigned_name = employee.name
    db.session.commit()
    # The Copilot index stamps each chunk with the owner at index time.
    # Left alone, the previous owner kept finding these leads' emails
    # and notes in Copilot search after losing the leads themselves.
    _drop_copilot_chunks(moved)
    return {'assigned': len(rows), 'to': employee.name}


def _drop_copilot_chunks(lead_ids):
    if not lead_ids:
        return
    try:
        from app.models.copilot import CopilotChunk
    except ImportError:
        return
    CopilotChunk.query.filter(CopilotChunk.lead_id.in_(lead_ids)).delete(
        synchronize_session=False)
    db.session.commit()


def set_field(lead_ids, field, value, actor):
    """Change vertical or priority in bulk, validated against Master Data."""
    from app import Lead
    from app.master_data import service as md

    allowed_field = {'procam_vertical': 'vertical', 'stage': None}
    if field not in allowed_field:
        raise ValueError(f'{field} cannot be set in bulk')

    lookup = allowed_field[field]
    if field == 'stage':
        from app import STAGES_ALL
        if value not in STAGES_ALL:
            raise ValueError(f'"{value}" is not a lead stage')
    if lookup:
        allowed = {i.label for i in md.items(lookup)}
        if value not in allowed:
            raise ValueError(
                f'"{value}" is not a known {lookup} — add it under Master '
                f'Data first')

    rows = Lead.query.filter(Lead.id.in_(lead_ids)).all()
    _bulk_event('bulk.set_field', rows, actor, None,
                extra={'field': field, 'value': value})
    for lead in rows:
        setattr(lead, field, value)
    db.session.commit()
    if field == 'procam_vertical':
        # Chunks carry the vertical too; a vertical-scoped viewer must
        # stop (or start) finding these at the same moment.
        _drop_copilot_chunks([l.id for l in rows])
    return {'updated': len(rows), 'field': field, 'value': value}


def delete(lead_ids, reason, actor):
    """There is no longer a bulk delete. Closes them as Lost.

    §55-57 built this carefully — a reason required, a refusal
    where history would be destroyed, the audit committed before
    the deletion. None of that was the problem. The problem is that
    a lead which did not happen is a lead which did not happen, and
    an absence cannot be reported on, searched, or explained to
    whoever asks next year why the pipeline shrank.

    So the same set is closed instead. The signature and the
    refusal rules are unchanged, because every caller and every
    test depends on them, and `deleted` still counts what the
    action affected.
    """
    from app import Lead
    if not (reason or '').strip():
        raise ValueError('A reason is required')

    rows = Lead.query.filter(Lead.id.in_(lead_ids)).all()
    if not rows:
        return {'deleted': 0, 'closed': 0}

    counts = linked_counts([l.id for l in rows])
    blocked = close_blocked(counts)
    if blocked:
        raise PermissionError(
            'Bulk close refused — ' + '; '.join(blocked)
            + '. Close those individually if that is really what '
              'happened.')

    batch_ref = uuid.uuid4().hex[:12]
    from app.services import audit as audit_trail
    from app.services import task_autoclose

    for lead in rows:
        _audit(lead, 'close_lost', reason, actor, counts, batch_ref)
    _bulk_event('bulk.close_lost', rows, actor, reason, batch_ref)
    for lead in rows:
        was = lead.stage
        # Its own action, not another `lead.stage_change`: the
        # session listener already writes one of those for the
        # stage itself, and two rows with one name means whichever
        # is read first wins.
        audit_trail.record('lead.closed_lost', 'lead', lead.id,
                           actor=actor,
                           old={'stage': was},
                           new={'stage': 'Lost',
                                'lost_reason': reason[:200]},
                           reason=f'bulk close as Lost: {reason}',
                           strict=True)
        lead.stage = 'Lost'
        lead.lost_reason = reason[:200]
        lead.updated_at = datetime.utcnow()
        # Their open tasks go with them, as on any other close.
        task_autoclose.close_for_lead(lead, actor)
    db.session.commit()
    return {'deleted': 0, 'closed': len(rows), 'batch_ref': batch_ref}
