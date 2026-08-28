"""
v2026-08 — Pre-Sales Intelligence · Phases 2, 3 & 4 models.

Phase 2:  Project + ProjectUpdate + ProjectStageHistory
Phase 3:  ProjectAccount (m:n)  +  ProjectContact (m:n)
Phase 4:  OpportunitySourceLink  (source attribution audit)

All FKs land against existing tables (companies, contacts, opportunities,
employees). No table is dropped or renamed. Every field the spec called
"not always mandatory" is nullable.
"""
from datetime import datetime
from app import db


# ─── Reference vocabularies (admin-configurable via Phase 5) ───────────
PROJECT_STAGES = (
    'Project Identified', 'Announced / Proposed', 'Planning',
    'Approval / Funding', 'Tender Expected', 'EPC Tendering',
    'EPC Appointed', 'Procurement Started', 'Equipment Procurement',
    'Logistics Opportunity Identified', 'RFQ Expected',
    'RFQ Received', 'Execution', 'On Hold', 'Cancelled',
)

PROJECT_UPDATE_TYPES = (
    'Announcement', 'Approval', 'Funding', 'Tender', 'Bidder',
    'Contract Award', 'Supplier Identified', 'Procurement',
    'Order Placed', 'RFQ', 'Delay', 'Status Change',
    'Research', 'Internal Note', 'Other',
)

PROJECT_ACCOUNT_ROLES = (
    'Project Owner', 'Developer', 'EPC', 'PMC', 'Consultant',
    'Technology Provider', 'Equipment Manufacturer',
    'Package Contractor', 'Logistics Contractor', 'Vendor', 'Other',
)

OPPORTUNITY_SOURCE_TYPES = (
    'Direct Customer', 'Account Development', 'Project Intelligence',
    'Existing Customer', 'Referral', 'Overseas Partner',
    'Network Partner', 'Website', 'Email',
    'Management Reference', 'Other',
)


# ─── Phase 2 · Project Intelligence master ─────────────────────────────
class Project(db.Model):
    """A potential future project that may eventually generate logistics
    opportunities. Exists WITHOUT an RFQ. Example: an announced power
    project that hasn't yet appointed an EPC."""
    __tablename__ = 'projects'
    id                     = db.Column(db.Integer, primary_key=True)
    project_code           = db.Column(db.String(30), unique=True, nullable=True, index=True)
    name                   = db.Column(db.String(250), nullable=False, index=True)
    project_type           = db.Column(db.String(80),  nullable=True)   # Power / Refinery / Port …
    industry               = db.Column(db.String(120), nullable=True, index=True)
    country                = db.Column(db.String(60),  nullable=True, default='India')
    state                  = db.Column(db.String(80),  nullable=True, index=True)
    location               = db.Column(db.String(200), nullable=True)
    estimated_value_inr    = db.Column(db.Numeric(15, 2), nullable=True)
    project_capacity       = db.Column(db.String(120), nullable=True)   # e.g. "1200 MW"

    announcement_date      = db.Column(db.Date, nullable=True)
    expected_start_date    = db.Column(db.Date, nullable=True)
    expected_proc_start    = db.Column(db.Date, nullable=True)
    expected_construction  = db.Column(db.String(120), nullable=True)   # e.g. "18 months"

    stage                  = db.Column(db.String(60), default='Project Identified', index=True)
    procurement_status     = db.Column(db.String(60), nullable=True)
    source                 = db.Column(db.String(80), nullable=True)   # News / Referral / Site visit
    source_url             = db.Column(db.String(500), nullable=True)
    source_publication     = db.Column(db.String(200), nullable=True)
    source_date            = db.Column(db.Date, nullable=True)

    description            = db.Column(db.Text, nullable=True)
    logistics_potential    = db.Column(db.Text, nullable=True)
    procam_vertical        = db.Column(db.String(60), nullable=True)
    priority               = db.Column(db.String(20), default='Medium')

    pic_emp_code           = db.Column(db.String(20), nullable=True, index=True)
    branch                 = db.Column(db.String(60), nullable=True)

    last_update_at         = db.Column(db.DateTime, nullable=True, index=True)
    next_review_at         = db.Column(db.Date, nullable=True, index=True)
    remarks                = db.Column(db.Text, nullable=True)

    is_archived            = db.Column(db.Boolean, default=False, index=True)
    created_at             = db.Column(db.DateTime, default=datetime.utcnow)
    created_by             = db.Column(db.String(20), nullable=True)

    def to_dict(self) -> dict:
        return {
            'id':                 self.id,
            'project_code':       self.project_code or '',
            'name':               self.name,
            'project_type':       self.project_type or '',
            'industry':           self.industry or '',
            'country':            self.country or '',
            'state':              self.state or '',
            'location':           self.location or '',
            'estimated_value':    float(self.estimated_value_inr or 0),
            'project_capacity':   self.project_capacity or '',
            'announcement_date':  str(self.announcement_date) if self.announcement_date else '',
            'expected_start':     str(self.expected_start_date) if self.expected_start_date else '',
            'stage':              self.stage or '',
            'procurement_status': self.procurement_status or '',
            'source':             self.source or '',
            'source_url':         self.source_url or '',
            'source_date':        str(self.source_date) if self.source_date else '',
            'procam_vertical':    self.procam_vertical or '',
            'priority':           self.priority or '',
            'pic':                self.pic_emp_code or '',
            'branch':             self.branch or '',
            'last_update_at':     str(self.last_update_at)[:16] if self.last_update_at else '',
            'next_review_at':     str(self.next_review_at) if self.next_review_at else '',
            'description':        self.description or '',
            'logistics_potential':self.logistics_potential or '',
            'remarks':            self.remarks or '',
            'is_archived':        bool(self.is_archived),
        }


class ProjectUpdate(db.Model):
    """Chronological project-intelligence timeline. Never overwritten."""
    __tablename__ = 'project_updates'
    id              = db.Column(db.Integer, primary_key=True)
    project_id      = db.Column(db.Integer, db.ForeignKey('projects.id'),
                                nullable=False, index=True)
    update_date     = db.Column(db.Date, default=datetime.utcnow, index=True)
    update_type     = db.Column(db.String(40), default='Other')
    source          = db.Column(db.String(200), nullable=True)
    source_url      = db.Column(db.String(500), nullable=True)
    summary         = db.Column(db.Text, nullable=False)
    updated_by      = db.Column(db.String(20), nullable=False)  # emp_code
    attachment_path = db.Column(db.String(500), nullable=True)
    next_action     = db.Column(db.String(255), nullable=True)
    next_review_at  = db.Column(db.Date, nullable=True)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id':              self.id,
            'project_id':      self.project_id,
            'update_date':     str(self.update_date) if self.update_date else '',
            'update_type':     self.update_type or '',
            'source':          self.source or '',
            'source_url':      self.source_url or '',
            'summary':         self.summary or '',
            'updated_by':      self.updated_by or '',
            'next_action':     self.next_action or '',
            'next_review_at':  str(self.next_review_at) if self.next_review_at else '',
        }


class ProjectStageHistory(db.Model):
    """Project stage transition audit."""
    __tablename__ = 'project_stage_history'
    id            = db.Column(db.Integer, primary_key=True)
    project_id    = db.Column(db.Integer, db.ForeignKey('projects.id'),
                              nullable=False, index=True)
    from_stage    = db.Column(db.String(60))
    to_stage      = db.Column(db.String(60), nullable=False)
    changed_by    = db.Column(db.String(20), nullable=False)
    changed_at    = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    note          = db.Column(db.Text)


# ─── Phase 3 · Project ↔ Account / Contact mapping ─────────────────────
class ProjectAccount(db.Model):
    """One project may involve multiple organisations. Same organisation
    can appear more than once with different roles."""
    __tablename__ = 'project_accounts'
    id           = db.Column(db.Integer, primary_key=True)
    project_id   = db.Column(db.Integer, db.ForeignKey('projects.id'),
                             nullable=False, index=True)
    account_id   = db.Column(db.Integer, db.ForeignKey('companies.id'),
                             nullable=False, index=True)
    role         = db.Column(db.String(40), nullable=False,
                             default='Other')             # one of PROJECT_ACCOUNT_ROLES
    is_primary   = db.Column(db.Boolean, default=False)
    note         = db.Column(db.Text)
    added_by     = db.Column(db.String(20))
    added_at     = db.Column(db.DateTime, default=datetime.utcnow)


class ProjectContact(db.Model):
    """Link relevant contacts (from Account contacts) to a project."""
    __tablename__ = 'project_contacts'
    id             = db.Column(db.Integer, primary_key=True)
    project_id     = db.Column(db.Integer, db.ForeignKey('projects.id'),
                               nullable=False, index=True)
    contact_id     = db.Column(db.Integer, db.ForeignKey('contacts.id'),
                               nullable=False, index=True)
    role_on_project= db.Column(db.String(80), nullable=True)  # e.g. "Procurement Head"
    added_by       = db.Column(db.String(20))
    added_at       = db.Column(db.DateTime, default=datetime.utcnow)


# ─── Phase 4 · Opportunity source attribution audit ────────────────────
class OpportunitySourceLink(db.Model):
    """One-row-per-Opportunity audit of where it came from. Complements
    the (denormalised) source_* columns on Opportunity itself so we can
    tell the full story even if an Opportunity is later re-attributed."""
    __tablename__ = 'opportunity_source_links'
    id                = db.Column(db.Integer, primary_key=True)
    opportunity_id    = db.Column(db.Integer, db.ForeignKey('opportunities.id'),
                                  nullable=False, index=True)
    source_type       = db.Column(db.String(40), nullable=False)
    source_account_id = db.Column(db.Integer, db.ForeignKey('companies.id'),
                                  nullable=True, index=True)
    source_project_id = db.Column(db.Integer, db.ForeignKey('projects.id'),
                                  nullable=True, index=True)
    linked_by         = db.Column(db.String(20), nullable=False)
    linked_at         = db.Column(db.DateTime, default=datetime.utcnow)
    note              = db.Column(db.Text)
