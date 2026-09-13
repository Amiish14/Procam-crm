"""Audit of destructive actions — §57.

"For permanent deletion require: reason, confirmation, user, timestamp and
audit trail."

Written before the deletion happens, so the record survives even if the
delete itself partly fails.  A snapshot of what was destroyed is kept,
because after a permanent delete the row itself is the only thing that can
answer what was there.
"""
from datetime import datetime

from app import db


class DeletionAudit(db.Model):
    __tablename__ = 'deletion_audit'

    id           = db.Column(db.Integer, primary_key=True)
    entity_type  = db.Column(db.String(30), nullable=False, index=True)
    entity_id    = db.Column(db.Integer, nullable=False, index=True)
    action       = db.Column(db.String(16), nullable=False, index=True)
    reason       = db.Column(db.String(400))
    # What the record held, and what was linked to it, at the moment it
    # went. After a permanent delete this is all that remains.
    snapshot     = db.Column(db.JSON, default=dict)
    linked       = db.Column(db.JSON, default=dict)
    performed_by = db.Column(db.String(20), nullable=False, index=True)
    performed_at = db.Column(db.DateTime, default=datetime.utcnow,
                             index=True)
    batch_ref    = db.Column(db.String(40), index=True)

    def to_dict(self):
        return {
            'id': self.id, 'entity_type': self.entity_type,
            'entity_id': self.entity_id, 'action': self.action,
            'reason': self.reason or '', 'snapshot': self.snapshot or {},
            'linked': self.linked or {},
            'performed_by': self.performed_by,
            'performed_at': str(self.performed_at)[:16]
                            if self.performed_at else '',
            'batch_ref': self.batch_ref or '',
        }


class AuditEvent(db.Model):
    """One business action: who did what to which record, and what changed.

    The general trail. DeletionAudit keeps its fuller snapshot for
    destructive actions; every other change that matters to access,
    ownership, pipeline or configuration lands here. Written in the same
    transaction as the change wherever the caller allows, so a change and
    its record commit or roll back together.
    """
    __tablename__ = 'audit_events'

    id          = db.Column(db.Integer, primary_key=True)
    occurred_at = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False, index=True)
    #: emp_code of the signed-in user; 'system' for jobs and scripts.
    actor       = db.Column(db.String(20), nullable=False, index=True)
    actor_role  = db.Column(db.String(30))
    #: dotted verb, e.g. 'employee.update', 'lead.stage_change'
    action      = db.Column(db.String(60), nullable=False, index=True)
    entity_type = db.Column(db.String(40), nullable=False, index=True)
    entity_id   = db.Column(db.String(60), index=True)
    #: only the fields that changed, before and after. Secrets are never
    #: stored — audit.record() replaces them with a marker.
    old_value   = db.Column(db.JSON)
    new_value   = db.Column(db.JSON)
    reason      = db.Column(db.String(400))
    ip          = db.Column(db.String(64))
    user_agent  = db.Column(db.String(200))

    __table_args__ = (
        db.Index('ix_audit_events_entity', 'entity_type', 'entity_id'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'occurred_at': (self.occurred_at.isoformat(timespec='seconds')
                            if self.occurred_at else ''),
            'actor': self.actor, 'actor_role': self.actor_role or '',
            'action': self.action, 'entity_type': self.entity_type,
            'entity_id': self.entity_id or '',
            'old_value': self.old_value, 'new_value': self.new_value,
            'reason': self.reason or '', 'ip': self.ip or '',
        }
