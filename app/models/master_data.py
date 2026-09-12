"""Admin-configurable master data — §59-62.

Verticals, industries, relationship types, lost reasons and the rest were
hard-coded in Python lists or, worse, existed only as free text typed into
a column.  Adding a vertical therefore meant a developer and a deploy.

Two tables replace that: a registry of lists, and the items in them.  One
generic shape rather than a table per vocabulary, so a new master costs a
row rather than a migration.

§61 is enforced in the service layer: an item that historical records
already reference is deactivated, never deleted.
"""
from datetime import datetime

from app import db


class MasterList(db.Model):
    """One configurable vocabulary, e.g. 'vertical'."""
    __tablename__ = 'master_lists'

    id          = db.Column(db.Integer, primary_key=True)
    key         = db.Column(db.String(40), unique=True, nullable=False,
                            index=True)
    label       = db.Column(db.String(80), nullable=False)
    description = db.Column(db.Text)
    # A system list is wired into code paths by key; it can be edited but
    # not removed, because something imports it by name.
    is_system   = db.Column(db.Boolean, default=True)
    sort_order  = db.Column(db.Integer, default=0)

    def to_dict(self, items=None):
        return {
            'key': self.key, 'label': self.label,
            'description': self.description or '',
            'is_system': bool(self.is_system),
            'items': [i.to_dict() for i in (items or [])],
        }


class MasterItem(db.Model):
    """One value in a list.  `meta` carries per-list extras — a vertical's
    head, a priority's colour — without a column per vocabulary."""
    __tablename__ = 'master_items'

    id          = db.Column(db.Integer, primary_key=True)
    list_key    = db.Column(db.String(40), nullable=False, index=True)
    code        = db.Column(db.String(60), nullable=False)
    label       = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text)
    sort_order  = db.Column(db.Integer, default=0)
    is_active   = db.Column(db.Boolean, default=True, index=True)
    meta        = db.Column(db.JSON, default=dict)

    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    created_by  = db.Column(db.String(20))
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('list_key', 'code', name='uq_master_item'),
        db.Index('ix_master_item_list_active', 'list_key', 'is_active'),
    )

    def to_dict(self):
        return {
            'id': self.id, 'code': self.code, 'label': self.label,
            'description': self.description or '',
            'sort_order': self.sort_order or 0,
            'is_active': bool(self.is_active),
            'meta': self.meta or {},
        }

    def __repr__(self):
        return f'<MasterItem {self.list_key}:{self.code}>'


# The lists the CRM ships with.  Adding one here makes it configurable;
# adding a *value* never needs code.
SYSTEM_LISTS = [
    ('vertical',        'Vertical',
     'Business verticals. Drives assignment, reporting and data scope.'),
    ('industry',        'Industry',
     'Customer industry classification.'),
    ('relationship',    'Relationship Type',
     'How a company relates to Procam (§5). A company may hold several.'),
    ('network',         'Network',
     'Logistics network memberships, e.g. PCN, THLG.'),
    ('service',         'Service',
     'Services Procam quotes for.'),
    ('lost_reason',     'Lost Reason',
     'Captured when an opportunity is lost.'),
    ('intel_type',      'Competitor Intelligence Type',
     'Categories for competitor intelligence entries (§20).'),
    ('assignment_role', 'Assignment Role',
     'Roles a person can hold on a record (§23). Never PIC1/PIC2/PIC3.'),
    ('task_type',       'Task Type',   'Categories of task.'),
    ('priority',        'Priority',    'Priority levels.'),
    ('source',          'Lead Source', 'Where an opportunity came from.'),
    ('lead_rejection_reason', 'Lead Rejection Reason',
     'Why a system-created lead was rejected. Each reason is a training '
     'signal, so the list is deliberately specific — "Other" is the only '
     'one that teaches nothing.'),
    ('project_stage',   'Project Stage',  'Project intelligence stages.'),
    ('account_stage',   'Account Stage',  'Account development stages.'),
]
