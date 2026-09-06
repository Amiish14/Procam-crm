"""
Universal Task Engine — data model (Phase 3 of the CRM upgrade).

Copied from the TMS `/Users/.../Procam-lr-main/app/models/task_engine.py`
with two adaptations for CRM's single-file Flask shape:

  1. `from app.extensions import db` → `from app import db`.
  2. Owner/user FKs point at `employees.emp_code` (String) instead of
     TMS `users.id` (Integer), because CRM identifies users by emp_code.

Two tables:
  task_definitions — the Master Task Catalogue as data.  Editable by
                     Admin.  Seeded by scripts/2026_09_04_crm_foundation.py.
  task_instances   — runtime rows.  One per pending task per owner-user.
                     Created by app.services.task_engine.on_state_change().
"""
from datetime import datetime
from sqlalchemy import Index

from app import db


class TaskDefinition(db.Model):
    """The Master Task Catalogue (§8 of the spec) as data."""
    __tablename__ = 'task_definitions'

    id = db.Column(db.Integer, primary_key=True)
    task_key = db.Column(db.String(80), unique=True, nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    module = db.Column(db.String(40), nullable=False, index=True)
    description = db.Column(db.Text, nullable=True)

    # Roles that can execute this task (JSON list of role keys).
    allowed_roles = db.Column(db.JSON, nullable=False, default=list)

    # Trigger conditions: which entity + which state creates this task.
    # e.g. {"entity": "Lead", "state": "RFQ Generated"}
    start_state = db.Column(db.JSON, nullable=True)
    # Which entity + state marks this task as done.
    completion_state = db.Column(db.JSON, nullable=True)

    # After completion, which task is created next (nullable = terminal).
    next_task_key = db.Column(db.String(80), nullable=True)
    # How to resolve the owner of the next task.  Simple form:
    #   {"kind": "role", "role": "Rate_Sourcing"}
    #   {"kind": "role_any", "roles": ["Rate_Sourcing", "Commercial_Support"]}
    #   {"kind": "primary_owner"}
    #   {"kind": "field", "path": "assigned_to"}
    #   {"kind": "user", "user_id": "E101"}
    next_owner_rule = db.Column(db.JSON, nullable=True)

    # The URL to open when the user clicks the task.  Supports {entity_id}.
    action_route = db.Column(db.String(200), nullable=True)

    # Due-date SLA in hours.  Null = no SLA.
    sla_hours = db.Column(db.Integer, nullable=True)
    # Escalation: role to notify when SLA breaches.
    escalation_role = db.Column(db.String(40), nullable=True)

    # Priority hint: 1 (critical) → 5 (informational).  Default 3.
    priority = db.Column(db.SmallInteger, nullable=False, default=3)

    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    def __repr__(self):
        return f'<TaskDefinition {self.task_key} ({self.module})>'


class TaskInstanceStatus:
    PENDING = 'Pending'
    IN_PROGRESS = 'In Progress'
    COMPLETED = 'Completed'
    CANCELLED = 'Cancelled'
    ESCALATED = 'Escalated'
    RETURNED = 'Returned'
    BLOCKED = 'Blocked'
    ALL = [PENDING, IN_PROGRESS, COMPLETED, CANCELLED, ESCALATED, RETURNED, BLOCKED]
    OPEN = [PENDING, IN_PROGRESS, ESCALATED, RETURNED, BLOCKED]
    CLOSED = [COMPLETED, CANCELLED]


class TaskInstance(db.Model):
    """Runtime task ownership. One row per pending task per owner-user."""
    __tablename__ = 'task_instances'

    id = db.Column(db.Integer, primary_key=True)
    task_key = db.Column(db.String(80), nullable=False, index=True)

    # The entity this task is about.
    entity_type = db.Column(db.String(40), nullable=False, index=True)
    entity_id = db.Column(db.Integer, nullable=False)
    entity_display = db.Column(db.String(120), nullable=True)

    # Who owns this task right now — user-level (emp_code) OR role-level
    # (any user whose Employee.role or member-role matches).
    owner_user_id = db.Column(db.String(20),
                              db.ForeignKey('employees.emp_code'),
                              nullable=True, index=True)
    owner_role = db.Column(db.String(40), nullable=True, index=True)

    status = db.Column(db.String(20), nullable=False,
                       default=TaskInstanceStatus.PENDING, index=True)
    priority = db.Column(db.SmallInteger, nullable=False, default=3)

    # Where the user should click to act on this task.
    action_route = db.Column(db.String(300), nullable=True)

    # Timestamps.
    created_at = db.Column(db.DateTime, nullable=False,
                           default=datetime.utcnow, index=True)
    due_at = db.Column(db.DateTime, nullable=True, index=True)
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    completed_by_id = db.Column(db.String(20),
                                db.ForeignKey('employees.emp_code'),
                                nullable=True)

    # If this task is a "return for correction" — pointer to original.
    parent_instance_id = db.Column(db.Integer,
                                   db.ForeignKey('task_instances.id'),
                                   nullable=True)
    return_reason = db.Column(db.Text, nullable=True)

    notes = db.Column(db.Text, nullable=True)

    # Blocked/Waiting metadata
    block_reason = db.Column(db.Text, nullable=True)
    waiting_from_user_id = db.Column(db.String(20),
                                     db.ForeignKey('employees.emp_code'),
                                     nullable=True)
    waiting_from_task_id = db.Column(db.Integer,
                                     db.ForeignKey('task_instances.id'),
                                     nullable=True)
    blocked_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        Index('ix_task_inst_open',
              'task_key', 'entity_type', 'entity_id',
              'owner_user_id', 'status'),
        Index('ix_task_inst_owner_status', 'owner_user_id', 'status'),
        Index('ix_task_inst_role_status', 'owner_role', 'status'),
        Index('ix_task_inst_entity', 'entity_type', 'entity_id'),
    )

    @property
    def is_open(self):
        return self.status in TaskInstanceStatus.OPEN

    @property
    def is_overdue(self):
        return (self.due_at is not None
                and self.status in TaskInstanceStatus.OPEN
                and datetime.utcnow() > self.due_at)

    @property
    def age_hours(self):
        return (datetime.utcnow() - self.created_at).total_seconds() / 3600.0

    def to_dict(self):
        return {
            'id':             self.id,
            'task_key':       self.task_key,
            'entity_type':    self.entity_type,
            'entity_id':      self.entity_id,
            'entity_display': self.entity_display or '',
            'owner_user_id':  self.owner_user_id or '',
            'owner_role':     self.owner_role or '',
            'status':         self.status,
            'priority':       self.priority,
            'action_route':   self.action_route or '',
            'created_at':     str(self.created_at)[:16] if self.created_at else '',
            'due_at':         str(self.due_at)[:16] if self.due_at else '',
            'completed_at':   str(self.completed_at)[:16] if self.completed_at else '',
            'notes':          self.notes or '',
            'is_overdue':     self.is_overdue,
        }

    def __repr__(self):
        return (f'<TaskInstance {self.task_key} '
                f'{self.entity_type}#{self.entity_id} → '
                f'{self.owner_user_id or self.owner_role} [{self.status}]>')
