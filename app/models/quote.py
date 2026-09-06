"""
Quote Management + Quote Revision — data model (Phase 7 of the CRM upgrade).

Three tables:

    quotes                — a quote sent (or being prepared) for a customer.
    quote_lines           — line items in the quote.
    quote_revision_logs   — audit trail of revisions.

Revisions: A quote can be "revised". The old row is marked `Superseded`,
a new row is created with `parent_quote_id` pointing back and
`revision_number` incremented. Lines are copied to the new revision.
A row in `quote_revision_logs` records the change reason + diff summary.
"""
from datetime import date, datetime

from app import db


class Quote(db.Model):
    __tablename__ = 'quotes'

    id             = db.Column(db.Integer, primary_key=True)
    quote_number   = db.Column(db.String(40), unique=True, nullable=False, index=True)

    rfq_id         = db.Column(db.Integer, db.ForeignKey('rfqs.id'),
                               nullable=True, index=True)
    opportunity_id = db.Column(db.Integer, db.ForeignKey('opportunities.id'),
                               nullable=True, index=True)
    lead_id        = db.Column(db.Integer, db.ForeignKey('leads.id'),
                               nullable=True, index=True)
    account_id     = db.Column(db.Integer, db.ForeignKey('companies.id'),
                               nullable=True, index=True)

    subject        = db.Column(db.String(240))
    quote_date     = db.Column(db.Date, default=date.today)
    validity_until = db.Column(db.Date)
    currency       = db.Column(db.String(6), default='INR')
    subtotal       = db.Column(db.Numeric(15, 2), default=0)
    tax_amount     = db.Column(db.Numeric(15, 2), default=0)
    total_amount   = db.Column(db.Numeric(15, 2), default=0)
    payment_terms  = db.Column(db.Text)
    inclusions     = db.Column(db.Text)
    exclusions     = db.Column(db.Text)
    remarks        = db.Column(db.Text)

    # Draft / Awaiting Approval / Approved / Submitted /
    # Under Negotiation / Won / Lost / Superseded / Withdrawn
    status         = db.Column(db.String(24), default='Draft',
                               index=True, nullable=False)

    prepared_by_id  = db.Column(db.String(20))
    approved_by_id  = db.Column(db.String(20))
    approved_at     = db.Column(db.DateTime)
    submitted_by_id = db.Column(db.String(20))
    submitted_at    = db.Column(db.DateTime)
    won_at          = db.Column(db.DateTime)
    lost_at         = db.Column(db.DateTime)
    lost_reason     = db.Column(db.String(200))

    parent_quote_id = db.Column(db.Integer, db.ForeignKey('quotes.id'),
                                nullable=True)
    revision_number = db.Column(db.Integer, default=1)

    created_by_id   = db.Column(db.String(20))
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at      = db.Column(db.DateTime, default=datetime.utcnow,
                                onupdate=datetime.utcnow)

    def recompute_totals(self):
        """Sum QuoteLine.line_total for this quote and refresh totals."""
        try:
            from decimal import Decimal
            rows = QuoteLine.query.filter_by(quote_id=self.id).all()
            subtotal = sum((r.line_total or Decimal('0')) for r in rows) \
                       if rows else Decimal('0')
            self.subtotal = subtotal
            tax = self.tax_amount or Decimal('0')
            self.total_amount = subtotal + tax
        except Exception:
            pass

    def to_dict(self, *, deep=False):
        d = {
            'id':               self.id,
            'quote_number':     self.quote_number,
            'rfq_id':           self.rfq_id,
            'opportunity_id':   self.opportunity_id,
            'lead_id':          self.lead_id,
            'account_id':       self.account_id,
            'subject':          self.subject or '',
            'quote_date':       str(self.quote_date) if self.quote_date else '',
            'validity_until':   str(self.validity_until) if self.validity_until else '',
            'currency':         self.currency or 'INR',
            'subtotal':         float(self.subtotal or 0),
            'tax_amount':       float(self.tax_amount or 0),
            'total_amount':     float(self.total_amount or 0),
            'payment_terms':    self.payment_terms or '',
            'inclusions':       self.inclusions or '',
            'exclusions':       self.exclusions or '',
            'remarks':          self.remarks or '',
            'status':           self.status,
            'prepared_by_id':   self.prepared_by_id or '',
            'approved_by_id':   self.approved_by_id or '',
            'approved_at':      str(self.approved_at)[:19] if self.approved_at else '',
            'submitted_by_id':  self.submitted_by_id or '',
            'submitted_at':     str(self.submitted_at)[:19] if self.submitted_at else '',
            'won_at':           str(self.won_at)[:19] if self.won_at else '',
            'lost_at':          str(self.lost_at)[:19] if self.lost_at else '',
            'lost_reason':      self.lost_reason or '',
            'parent_quote_id':  self.parent_quote_id,
            'revision_number':  self.revision_number or 1,
            'created_by_id':    self.created_by_id or '',
            'created_at':       str(self.created_at)[:19] if self.created_at else '',
            'updated_at':       str(self.updated_at)[:19] if self.updated_at else '',
        }
        if deep:
            d['lines'] = [ln.to_dict() for ln in QuoteLine.query
                          .filter_by(quote_id=self.id)
                          .order_by(QuoteLine.line_no.asc(),
                                    QuoteLine.id.asc()).all()]
            d['revisions'] = [r.to_dict() for r in QuoteRevisionLog.query
                              .filter_by(quote_id=self.id)
                              .order_by(QuoteRevisionLog.changed_at.desc()).all()]
        return d


class QuoteLine(db.Model):
    __tablename__ = 'quote_lines'

    id         = db.Column(db.Integer, primary_key=True)
    quote_id   = db.Column(db.Integer,
                           db.ForeignKey('quotes.id', ondelete='CASCADE'),
                           nullable=False, index=True)
    line_no    = db.Column(db.Integer)
    service    = db.Column(db.String(80))
    description= db.Column(db.Text)
    quantity   = db.Column(db.Numeric(12, 3), default=1)
    unit       = db.Column(db.String(24))
    unit_rate  = db.Column(db.Numeric(15, 4))
    line_total = db.Column(db.Numeric(15, 2))
    remarks    = db.Column(db.Text)

    def recompute_total(self):
        from decimal import Decimal
        try:
            q = self.quantity or Decimal('0')
            r = self.unit_rate or Decimal('0')
            self.line_total = (Decimal(q) * Decimal(r)).quantize(Decimal('0.01'))
        except Exception:
            self.line_total = Decimal('0')

    def to_dict(self):
        return {
            'id':          self.id,
            'quote_id':    self.quote_id,
            'line_no':     self.line_no,
            'service':     self.service or '',
            'description': self.description or '',
            'quantity':    float(self.quantity or 0),
            'unit':        self.unit or '',
            'unit_rate':   float(self.unit_rate or 0),
            'line_total':  float(self.line_total or 0),
            'remarks':     self.remarks or '',
        }


class QuoteRevisionLog(db.Model):
    __tablename__ = 'quote_revision_logs'

    id            = db.Column(db.Integer, primary_key=True)
    quote_id      = db.Column(db.Integer, db.ForeignKey('quotes.id'),
                              nullable=False, index=True)
    from_revision = db.Column(db.Integer)
    to_revision   = db.Column(db.Integer)
    changed_by_id = db.Column(db.String(20))
    changed_at    = db.Column(db.DateTime, default=datetime.utcnow)
    diff_summary  = db.Column(db.JSON)   # {field: [old, new], ...}
    reason        = db.Column(db.Text)

    def to_dict(self):
        return {
            'id':            self.id,
            'quote_id':      self.quote_id,
            'from_revision': self.from_revision,
            'to_revision':   self.to_revision,
            'changed_by_id': self.changed_by_id or '',
            'changed_at':    str(self.changed_at)[:19] if self.changed_at else '',
            'diff_summary':  self.diff_summary or {},
            'reason':        self.reason or '',
        }
