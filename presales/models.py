"""
v2026-08 — Pre-Sales Intelligence Layer · Phase 1 models.

Only NEW tables live here. Extensions to existing Company/Contact/Opportunity
are added in app.py alongside the original model definitions to keep the
SQLAlchemy metadata coherent.
"""
from datetime import datetime
from app import db                                                # existing app


# ─── Reference vocabularies (application-side; admin-configurable later) ─
ACCOUNT_TYPES = (
    'Customer', 'Prospect', 'Project Owner', 'EPC', 'Consultant',
    'Manufacturer', 'Overseas Partner', 'Agent', 'Vendor', 'Supplier',
    'Competitor', 'Other',
)

ACCOUNT_DEV_STAGES = (
    'Target Identified', 'Researching', 'Contact Identification',
    'Initial Outreach', 'Contact Established', 'Meeting Planned',
    'Meeting Completed', 'Relationship Development',
    'Opportunity Identified', 'RFQ Expected', 'Active Account',
    'Dormant',
)

ACTIVITY_KINDS = (
    'Research', 'LinkedIn Outreach', 'Email', 'Phone Call', 'Meeting',
    'Visit', 'Introduction', 'Network Referral', 'Contact Identified',
    'Project Identified', 'Follow-up', 'RFQ Received', 'Quotation',
    'Relationship Note', 'Internal Note', 'Other',
)


class AccountRelationshipTag(db.Model):
    """Many-to-many role tag on an Account. One Account may carry any
    combination — e.g. an overseas company that is both Partner + Vendor +
    RFQ Source."""
    __tablename__ = 'account_relationship_tags'
    id            = db.Column(db.Integer, primary_key=True)
    account_id    = db.Column(db.Integer, db.ForeignKey('companies.id'),
                              nullable=False, index=True)
    tag           = db.Column(db.String(40), nullable=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint('account_id', 'tag', name='uq_acct_tag'),
    )


class AccountAssignmentHistory(db.Model):
    """PIC change audit. Never overwritten — every reassignment appends
    a row so the next PIC can see the full handover chain."""
    __tablename__ = 'account_assignments'
    id                = db.Column(db.Integer, primary_key=True)
    account_id        = db.Column(db.Integer, db.ForeignKey('companies.id'),
                                  nullable=False, index=True)
    previous_pic_code = db.Column(db.String(20))                  # nullable at first assignment
    new_pic_code      = db.Column(db.String(20), nullable=False)
    assigned_by       = db.Column(db.String(20), nullable=False)  # emp_code of the person who made the change
    reason            = db.Column(db.Text)
    assigned_at       = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class AccountStageHistory(db.Model):
    """Dev-stage transition audit. Populated by services.change_stage()."""
    __tablename__ = 'account_stage_history'
    id           = db.Column(db.Integer, primary_key=True)
    account_id   = db.Column(db.Integer, db.ForeignKey('companies.id'),
                             nullable=False, index=True)
    from_stage   = db.Column(db.String(60))
    to_stage     = db.Column(db.String(60), nullable=False)
    changed_by   = db.Column(db.String(20), nullable=False)       # emp_code
    changed_at   = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    note         = db.Column(db.Text)


class AccountActivity(db.Model):
    """Chronological activity timeline on an Account. Belongs to the
    Account, not to the salesperson — reassignment preserves it."""
    __tablename__ = 'account_activities'
    id               = db.Column(db.Integer, primary_key=True)
    account_id       = db.Column(db.Integer, db.ForeignKey('companies.id'),
                                 nullable=False, index=True)
    contact_id       = db.Column(db.Integer, db.ForeignKey('contacts.id'),
                                 nullable=True, index=True)
    kind             = db.Column(db.String(40), nullable=False,
                                 default='Internal Note')          # one of ACTIVITY_KINDS
    subject          = db.Column(db.String(255))
    body             = db.Column(db.Text)
    occurred_at      = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    performed_by     = db.Column(db.String(20), nullable=False)    # emp_code
    next_action      = db.Column(db.String(255))
    next_action_at   = db.Column(db.Date)
    attachment_path  = db.Column(db.String(500))
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            'id':             self.id,
            'account_id':     self.account_id,
            'contact_id':     self.contact_id,
            'kind':           self.kind,
            'subject':        self.subject or '',
            'body':           self.body or '',
            'occurred_at':    str(self.occurred_at)[:16] if self.occurred_at else '',
            'performed_by':   self.performed_by or '',
            'next_action':    self.next_action or '',
            'next_action_at': str(self.next_action_at) if self.next_action_at else '',
        }
