"""Audit every change to the records that matter, whichever path made it.

A route can forget to call audit.record(); a bulk tool, a script or a
service written next year certainly will. So the trail is kept at the
database session instead: before each flush, the watched fields of the
watched models are compared with what was loaded, and an AuditEvent is
added for every create, change and delete — in the same flush, so the
change and its record commit or roll back together.

What this cannot see: Query.update() / Query.delete() and raw SQL, which
bypass the ORM. Those paths call audit.record() explicitly (bulk admin,
imports), and tests hold them to it.

Actions are named by what changed, so the trail can be filtered by
meaning: a lead whose stage and owner both changed in one save produces
'lead.stage_change' and 'lead.ownership_change'.

A reason for the change can be attached by the route that makes it:

    from flask import g
    g.audit_reason = 'Customer asked for a specialist'
"""
import logging

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from app.services import audit

log = logging.getLogger(__name__)

#: model name → (entity_type, {field: action}, default action for the rest)
#: Only these fields are compared; everything else is not audit material.
WATCH = {
    'Lead': ('lead', {
        'stage': 'lead.stage_change',
        'assigned_to': 'lead.ownership_change',
        'secondary_owner': 'lead.ownership_change',
        'is_archived': 'lead.archive_change',
        'archive_reason': 'lead.archive_change',
        'procam_vertical': 'lead.update',
        'company_id': 'lead.update',
        'company': 'lead.update',
        'estimated_value_inr': 'lead.update',
        'quoted_amount_inr': 'lead.update',
        'lost_reason': 'lead.update',
        'classification': 'lead.update',
    }),
    'Opportunity': ('opportunity', {
        'stage': 'opportunity.stage_change',
        'owner_emp_code': 'opportunity.ownership_change',
        'value_inr': 'opportunity.update',
        'probability': 'opportunity.update',
        'expected_close_date': 'opportunity.update',
        'won_at': 'opportunity.update',
        'lost_at': 'opportunity.update',
        'lost_reason': 'opportunity.update',
        'company_id': 'opportunity.update',
        'won_project_ref': 'opportunity.update',
    }),
    'Company': ('company', {
        'pic_emp_code': 'company.ownership_change',
        'secondary_pic_emp_code': 'company.ownership_change',
        'backup_pic_emp_code': 'company.ownership_change',
        'name': 'company.update',
        'vertical': 'company.update',
        'gstin': 'company.update',
        'is_active': 'company.update',
        'email_domains': 'company.update',
        'parent_account_id': 'company.update',
        'dev_stage': 'company.update',
        'tier': 'company.update',
    }),
    'Contact': ('contact', {
        'assigned_to': 'contact.ownership_change',
        'company_id': 'contact.update',
        'name': 'contact.update',
        'email': 'contact.update',
        'phone': 'contact.update',
        'mobile': 'contact.update',
        'is_active': 'contact.update',
    }),
    'Employee': ('employee', {
        'role': 'employee.role_change',
        'is_super_admin': 'employee.role_change',
        'is_vertical_head': 'employee.role_change',
        'is_active': 'employee.activation_change',
        'password_hash': 'employee.password_change',
        'must_change_pw': 'employee.password_change',
        'vertical': 'employee.update',
        'department': 'employee.update',
        'designation': 'employee.update',
        'email': 'employee.update',
        'name': 'employee.update',
        'vertical_head_id': 'employee.update',
    }),
    'AccessProfile': ('access_profile', {
        'data_scope': 'access.permission_change',
        'perms': 'access.permission_change',
    }),
    'MasterItem': ('master_item', {
        'label': 'config.master_data_change',
        'code': 'config.master_data_change',
        'is_active': 'config.master_data_change',
        'list_key': 'config.master_data_change',
    }),
    'VendorDomain': ('vendor_domain', {
        'domain': 'config.vendor_change',
        'vendor_type': 'config.vendor_change',
        'is_active': 'config.vendor_change',
    }),
    'LeadNote': ('lead_note', {
        'note_text': 'note.update',
        'is_deleted': 'note.delete',
    }),
    'EmailClassification': ('email_classification', {
        'corrected_to': 'classifier.correction',
        'review_state': 'classifier.review',
        'created_lead_id': 'classifier.review',
    }),
    'RFQ': ('rfq', {'status': 'rfq.status_change',
                    'lead_driver': 'rfq.ownership_change'}),
    'Quote': ('quote', {'status': 'quote.status_change',
                        'total_amount': 'quote.update',
                        'approved_by_id': 'quote.approval'}),
    'WonHandover': ('handover', {'status': 'handover.status_change',
                                 'po_ref': 'handover.po_change',
                                 'po_value': 'handover.po_change'}),
    'OverseasAgent': ('overseas_agent', {}),
}

def _reason():
    try:
        from flask import g, has_app_context
        if has_app_context():
            return getattr(g, 'audit_reason', None)
    except Exception:                               # pragma: no cover
        pass
    return None


def _pk(obj):
    # People are known by their employee code, not a row number — an
    # audit reader searching for DIR10001 should find every change to them.
    code = getattr(obj, 'emp_code', None)
    if code:
        return code
    try:
        ident = inspect(obj).identity
        if ident:
            return ident[0] if len(ident) == 1 else '-'.join(map(str, ident))
    except Exception:
        pass
    return getattr(obj, 'emp_code', None) or getattr(obj, 'id', None)


def _value(v, field):
    return audit._clean(v, field)


def _events_for_dirty(obj, spec):
    entity_type, fields = spec
    state = inspect(obj)
    grouped = {}
    for field, action in fields.items():
        if field not in state.attrs:
            continue
        hist = state.attrs[field].history
        if not hist.has_changes():
            continue
        old = hist.deleted[0] if hist.deleted else None
        new = hist.added[0] if hist.added else None
        if old == new:
            continue
        if isinstance(old, (list, dict)) and isinstance(new, (list, dict)) \
                and old == new:
            continue
        grouped.setdefault(action, ({}, {}))
        if field == 'password_hash':
            # That it changed is the record; the hash itself never is.
            # (A key naming a password would be redacted wholesale.)
            grouped[action][0]['sign_in_details'] = 'previous'
            grouped[action][1]['sign_in_details'] = 'changed'
            continue
        grouped[action][0][field] = _value(old, field)
        grouped[action][1][field] = _value(new, field)
    return [(action, entity_type, olds, news)
            for action, (olds, news) in grouped.items()]


def _snapshot(obj, spec):
    entity_type, fields = spec
    out = {}
    for field in fields:
        if field == 'password_hash':
            continue
        if hasattr(obj, field):
            out[field] = _value(getattr(obj, field), field)
    return out


@event.listens_for(Session, 'before_flush')
def _audit_before_flush(session, flush_context, instances):
    if session.info.get('audit_disabled'):
        return
    try:
        from app.models.audit import AuditEvent
    except Exception:                               # pragma: no cover
        return
    pending = []
    try:
        for obj in list(session.new):
            if isinstance(obj, AuditEvent):
                continue
            spec = WATCH.get(type(obj).__name__)
            if spec:
                pending.append((obj, f'{spec[0]}.create', spec[0],
                                None, _snapshot(obj, spec)))
        for obj in list(session.dirty):
            spec = WATCH.get(type(obj).__name__)
            if not spec or not session.is_modified(obj,
                                                   include_collections=False):
                continue
            for action, etype, old, new in _events_for_dirty(obj, spec):
                pending.append((obj, action, etype, old, new))
        for obj in list(session.deleted):
            spec = WATCH.get(type(obj).__name__)
            if spec:
                pending.append((obj, f'{spec[0]}.delete', spec[0],
                                _snapshot(obj, spec), None))
    except Exception:
        log.exception('audit listener could not read the flush')
        return

    reason = _reason()
    for obj, action, etype, old, new in pending:
        # New rows have no primary key before the flush; remember the
        # object and fill the id in after it.
        ev = audit.record(action, etype, _pk(obj), old=old, new=new,
                          reason=reason, session=session)
        if ev is not None and ev.entity_id is None:
            session.info.setdefault('audit_fill_ids', []).append((ev, obj))


@event.listens_for(Session, 'after_flush')
def _audit_fill_ids(session, flush_context):
    pending = session.info.pop('audit_fill_ids', None)
    if not pending:
        return
    for ev, obj in pending:
        pk = _pk(obj)
        if pk is not None:
            # Written with a direct UPDATE: changing the event object here
            # would need another flush inside this one.
            try:
                session.execute(
                    ev.__table__.update()
                    .where(ev.__table__.c.id == ev.id)
                    .values(entity_id=str(pk)[:60]))
            except Exception:
                log.exception('audit could not record the new id')
