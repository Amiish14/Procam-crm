"""
Won → TMS Handover — data model (Phase 8 of the CRM upgrade).

One row per Won opportunity/quote handed off to Transport Management.
Auto-created by the state-change hook when an opportunity or quote is
marked Won; TMS-side users then fill in tms_project_id/tms_job_id and
acknowledge, at which point the row advances to `TMS Project Created`.
"""
from datetime import datetime

from app import db


class WonHandover(db.Model):
    __tablename__ = 'won_handovers'

    id             = db.Column(db.Integer, primary_key=True)

    opportunity_id = db.Column(db.Integer, db.ForeignKey('opportunities.id'),
                               nullable=True, index=True)
    quote_id       = db.Column(db.Integer, db.ForeignKey('quotes.id'),
                               nullable=True, index=True)
    rfq_id         = db.Column(db.Integer, db.ForeignKey('rfqs.id'),
                               nullable=True, index=True)
    account_id     = db.Column(db.Integer, db.ForeignKey('companies.id'),
                               nullable=True, index=True)
    project_id     = db.Column(db.Integer, db.ForeignKey('projects.id'),
                               nullable=True, index=True)

    # Snapshot of what's being handed over — captured at Won time so a
    # later edit to the source Lead/Opp doesn't rewrite history.
    account_name    = db.Column(db.String(240))
    won_value       = db.Column(db.Numeric(15, 2))
    services        = db.Column(db.JSON)
    origin          = db.Column(db.String(120))
    destination     = db.Column(db.String(120))
    scope           = db.Column(db.Text)
    vertical        = db.Column(db.String(80))
    pic_emp_code    = db.Column(db.String(20))
    po_ref          = db.Column(db.String(80))
    attachments     = db.Column(db.JSON, default=list)   # [{name, path}, ...]
    commercial_refs = db.Column(db.JSON, default=dict)

    # Handover Pending / TMS Project Created / Handover Complete / Cancelled
    status         = db.Column(db.String(24), default='Handover Pending',
                               index=True, nullable=False)

    tms_project_id = db.Column(db.String(40))
    tms_job_id     = db.Column(db.String(40))
    tms_created_at = db.Column(db.DateTime)
    tms_ack_by     = db.Column(db.String(80))
    tms_ack_at     = db.Column(db.DateTime)
    remarks        = db.Column(db.Text)

    created_by_id  = db.Column(db.String(20))
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at     = db.Column(db.DateTime, default=datetime.utcnow,
                               onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id':              self.id,
            'opportunity_id':  self.opportunity_id,
            'quote_id':        self.quote_id,
            'rfq_id':          self.rfq_id,
            'account_id':      self.account_id,
            'project_id':      self.project_id,
            'account_name':    self.account_name or '',
            'won_value':       float(self.won_value or 0),
            'services':        list(self.services or []),
            'origin':          self.origin or '',
            'destination':     self.destination or '',
            'scope':           self.scope or '',
            'vertical':        self.vertical or '',
            'pic_emp_code':    self.pic_emp_code or '',
            'po_ref':          self.po_ref or '',
            'attachments':     list(self.attachments or []),
            'commercial_refs': dict(self.commercial_refs or {}),
            'status':          self.status,
            'tms_project_id':  self.tms_project_id or '',
            'tms_job_id':      self.tms_job_id or '',
            'tms_created_at':  str(self.tms_created_at)[:19] if self.tms_created_at else '',
            'tms_ack_by':      self.tms_ack_by or '',
            'tms_ack_at':      str(self.tms_ack_at)[:19] if self.tms_ack_at else '',
            'remarks':         self.remarks or '',
            'created_by_id':   self.created_by_id or '',
            'created_at':      str(self.created_at)[:19] if self.created_at else '',
            'updated_at':      str(self.updated_at)[:19] if self.updated_at else '',
        }
