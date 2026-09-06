"""
Role-based Assignment Engine — data model (Phase 2 of the CRM upgrade).

Adds first-class *member* rows for Accounts / Deals / Leads / Projects
so several people can share a record while still identifying a single
Primary Owner.  The seven canonical CRM roles are seeded on first
migration; Admin may add more.

Design notes:
  * Legacy single-owner columns (`Lead.assigned_to`,
    `Company.pic_emp_code`, `Opportunity.owner_emp_code`,
    `Project.pic_emp_code`) are NOT deleted.  During transition the
    legacy value is treated as the Primary Owner.  The migration
    backfills a matching `is_primary=True` member row for every
    populated legacy field.
  * All FK user references use `String(20)` pointing at
    `employees.emp_code` — the CRM Employee model uses `emp_code` as
    the login handle even though its primary key is an integer `id`.
"""
from datetime import datetime

from app import db


class Role(db.Model):
    """A named CRM role that can be assigned to a member of any
    record type (Account / Deal / Lead / Project)."""
    __tablename__ = 'crm_roles'

    id           = db.Column(db.Integer, primary_key=True)
    key          = db.Column(db.String(60), unique=True, nullable=False, index=True)
    name         = db.Column(db.String(120), nullable=False)
    description  = db.Column(db.Text, nullable=True)
    is_system    = db.Column(db.Boolean, nullable=False, default=False)
    is_active    = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_at   = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'key': self.key, 'name': self.name,
            'description': self.description or '',
            'is_system': bool(self.is_system),
            'is_active': bool(self.is_active),
        }


# The 7 canonical roles seeded at boot / migration.
SEED_ROLES = [
    ('Lead_Driver',              'Lead Driver',
     'Primary sales owner driving the deal to closure.'),
    ('Rate_Sourcing',            'Rate Sourcing',
     'Sources rates from vendors / carriers for RFQs.'),
    ('Technical_Support',        'Technical Support',
     'Provides engineering / feasibility input for a deal.'),
    ('Vertical_Head',            'Vertical Head',
     'Vertical head — approves quotes and mentors the deal.'),
    ('Overseas_Partner_Manager', 'Overseas Partner Manager',
     'Coordinates with the overseas agent supplying the lead.'),
    ('Commercial_Support',       'Commercial Support',
     'Handles commercial / finance / documentation on the deal.'),
    ('Management_Sponsor',       'Management Sponsor',
     'Executive sponsor for strategic accounts / opportunities.'),
]


class _MemberMixin:
    """Shared shape for {Account,Deal,Lead,Project}Member.

    `user_id` is an emp_code (String) so it lines up with CRM's
    existing single-owner columns (`assigned_to`, `pic_emp_code`,
    `owner_emp_code`, `pic_emp_code`).
    """
    id             = db.Column(db.Integer, primary_key=True)
    user_id        = db.Column(db.String(20),
                               db.ForeignKey('employees.emp_code'),
                               nullable=False, index=True)
    role_id        = db.Column(db.Integer,
                               db.ForeignKey('crm_roles.id'),
                               nullable=False, index=True)
    is_primary     = db.Column(db.Boolean, nullable=False, default=False, index=True)
    assigned_by    = db.Column(db.String(20), nullable=True)
    assigned_at    = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    effective_from = db.Column(db.Date, nullable=True)
    effective_to   = db.Column(db.Date, nullable=True)
    is_active      = db.Column(db.Boolean, nullable=False, default=True, index=True)
    remarks        = db.Column(db.Text, nullable=True)

    def _base_dict(self):
        return {
            'id':             self.id,
            'user_id':        self.user_id,
            'role_id':        self.role_id,
            'is_primary':     bool(self.is_primary),
            'assigned_by':    self.assigned_by,
            'assigned_at':    str(self.assigned_at)[:16] if self.assigned_at else '',
            'effective_from': str(self.effective_from) if self.effective_from else '',
            'effective_to':   str(self.effective_to) if self.effective_to else '',
            'is_active':      bool(self.is_active),
            'remarks':        self.remarks or '',
        }


class AccountMember(_MemberMixin, db.Model):
    __tablename__ = 'crm_account_members'
    account_id = db.Column(db.Integer, db.ForeignKey('companies.id'),
                           nullable=False, index=True)

    def to_dict(self):
        d = self._base_dict()
        d['account_id'] = self.account_id
        return d


class DealMember(_MemberMixin, db.Model):
    __tablename__ = 'crm_deal_members'
    opportunity_id = db.Column(db.Integer,
                               db.ForeignKey('opportunities.id'),
                               nullable=False, index=True)

    def to_dict(self):
        d = self._base_dict()
        d['opportunity_id'] = self.opportunity_id
        return d


class LeadMember(_MemberMixin, db.Model):
    __tablename__ = 'crm_lead_members'
    lead_id = db.Column(db.Integer, db.ForeignKey('leads.id'),
                        nullable=False, index=True)

    def to_dict(self):
        d = self._base_dict()
        d['lead_id'] = self.lead_id
        return d


class ProjectMember(_MemberMixin, db.Model):
    __tablename__ = 'crm_project_members'
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'),
                           nullable=False, index=True)

    def to_dict(self):
        d = self._base_dict()
        d['project_id'] = self.project_id
        return d


# Mapping used by app.services.assignment to pick the right member table
# for a given record.  Each entry: (entity_key, model_class, fk_column).
MEMBER_MODELS = (
    ('company',     AccountMember, 'account_id'),
    ('account',     AccountMember, 'account_id'),
    ('opportunity', DealMember,    'opportunity_id'),
    ('deal',        DealMember,    'opportunity_id'),
    ('lead',        LeadMember,    'lead_id'),
    ('project',     ProjectMember, 'project_id'),
)
