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

    #: FF-COMMS-01 R1. The Incoterm, and which side of the shipment
    #: our client is on. Both are needed: the same term bills the
    #: opposite half depending on the answer.
    #: FF-COMMS-01 R2, carried from the enquiry.
    crn           = db.Column(db.String(24), index=True)

    #: The standard-terms version this quote printed under. Set
    #: once, never moved: the client has that wording in their hand.
    terms_version_id = db.Column(db.Integer, nullable=True)
    incoterm      = db.Column(db.String(8))
    #: The named place that completes the Incoterm — "FOB" alone is
    #: not a term, "FOB Chennai" is. Beside the Incoterm because
    #: they are one answer, not two.
    incoterm_place = db.Column(db.String(160))
    #: Set only when this job is billed differently from the way
    #: this customer usually is. Empty means "as the customer, and
    #: failing that as the vertical" — see tms_intake.
    gst_treatment = db.Column(db.String(24), nullable=True)
    client_party  = db.Column(db.String(12))

    parent_quote_id = db.Column(db.Integer, db.ForeignKey('quotes.id'),
                                nullable=True)
    revision_number = db.Column(db.Integer, default=1)

    # ── CRM-QUOTE-REV-02 (R3), D2 ──
    #: The first quote in the chain; equal to this row's own id for
    #: Rev-00. `parent_quote_id` already links a revision to the one
    #: it came from, which answers "what did this change" and not
    #: "which quotation is this" — a customer looking at Rev-03 has
    #: never seen the parent and knows only the first number.
    root_quote_id = db.Column(db.Integer, db.ForeignKey('quotes.id'),
                              index=True)
    #: 0 for the first. `revision_number` stays as it is, defaulting
    #: to 1, because 2,000 rows of reporting read it.
    rev_no = db.Column(db.Integer, default=0)
    #: Why this revision exists, from a Master Data list.
    revision_reason = db.Column(db.String(120))

    #: §5.3. What the job costs us, against `total_amount`, which
    #: is what we are charging. `QuoteLine.cost_amount` already
    #: holds a per-line cost for freight, where rate tables fill
    #: it in; this is the sheet total, which every other vertical
    #: types because it has no rate tables to read.
    #: §5.5 — where the PDF the customer actually received is kept,
    #: and its digest. On disk rather than in the row: a 200 KB
    #: blob per revision would bloat every backup of a table that
    #: is read constantly.
    sent_pdf_path = db.Column(db.String(600))
    sent_pdf_sha256 = db.Column(db.String(64))

    cost_amount = db.Column(db.Numeric(15, 2))
    cost_saved_at = db.Column(db.DateTime)
    cost_saved_by = db.Column(db.String(20))

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

    # FF-COMMS-01 R1.
    #: Which bucket of a forwarding quote this is. The Incoterm on
    #: the quote decides whether a group falls to our client — see
    #: app/services/ff_charges.py.
    charge_group = db.Column(db.String(24), index=True)
    #: What the leg costs us, from the vendor rate that was chosen.
    #: Without it there is no margin, and a forwarding quote without
    #: a margin is a price somebody guessed.
    cost_amount   = db.Column(db.Numeric(15, 2))
    cost_currency = db.Column(db.String(6))
    cost_inr      = db.Column(db.Numeric(15, 2))
    #: The vendor reply this line was priced from, so a quote can be
    #: traced back to the rate it rests on.
    source_rate_request_id = db.Column(db.Integer, index=True)
    #: Decision 21 Oct 2026 (H1). Own or Market, for hydraulic (PTM)
    #: lines: the minimum margin differs (38% own, 14% market). Blank on
    #: every other line. See app/services/margin_policy.py.
    vehicle_source = db.Column(db.String(10))

    @property
    def margin_inr(self):
        from decimal import Decimal
        if self.line_total is None or self.cost_inr is None:
            return None
        return Decimal(str(self.line_total)) - Decimal(str(self.cost_inr))

    @property
    def margin_pct(self):
        m = self.margin_inr
        if m is None or not self.line_total:
            return None
        return float(m) * 100.0 / float(self.line_total)

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
            'charge_group': self.charge_group or '',
            'cost_amount': (float(self.cost_amount)
                            if self.cost_amount is not None else None),
            'cost_currency': self.cost_currency or '',
            'cost_inr': (float(self.cost_inr)
                         if self.cost_inr is not None else None),
            'source_rate_request_id': self.source_rate_request_id,
            'vehicle_source': self.vehicle_source or '',
            'margin_inr': (float(self.margin_inr)
                           if self.margin_inr is not None else None),
            'margin_pct': self.margin_pct,
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


class UnmatchedQuoteMail(db.Model):
    """A quotation mail the CRM could not place, kept for a person.

    CRM-QUOTE-REV-02 §5.4.

    The spec says to reuse the FF-COMMS-01 unmatched queue. There
    is no queue to reuse: `ff_inbound.link` returns a reason to
    its caller and nothing persists it, so every unplaceable
    vendor reply since that release has been a log line. This is
    the queue, written once and used by both.

    The alternative — attaching on a best guess — is worse than a
    row here. A quotation filed on the wrong lead changes that
    lead's stage, its amount and its reported pipeline, and
    nobody looks at it again until the figures are wrong.
    """
    __tablename__ = 'unmatched_quote_mail'

    OPEN = 'open'
    LINKED = 'linked'
    NOT_A_QUOTE = 'not_a_quote'

    id = db.Column(db.Integer, primary_key=True)

    #: RFC-5322 Message-Id. Unique, so the same mail seen twice by
    #: the webhook and the poll is one row.
    message_id = db.Column(db.String(400), unique=True, index=True)
    conversation_id = db.Column(db.String(400), index=True)
    subject = db.Column(db.String(500))
    from_addr = db.Column(db.String(320), index=True)
    to_addr = db.Column(db.String(600))
    sent_at = db.Column(db.DateTime)
    body_excerpt = db.Column(db.Text)

    #: What the matcher did find, so a person starts from what we
    #: know rather than from nothing: a suggested lead from the
    #: same customer domain, and why it was not enough.
    suggested_lead_id = db.Column(db.Integer, index=True)
    suggested_account_id = db.Column(db.Integer, index=True)
    why = db.Column(db.String(300))

    status = db.Column(db.String(16), default=OPEN, index=True)
    resolved_by = db.Column(db.String(20))
    resolved_at = db.Column(db.DateTime)
    linked_lead_id = db.Column(db.Integer, index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           index=True)

    def to_dict(self):
        return {'id': self.id, 'subject': self.subject or '',
                'from_addr': self.from_addr or '',
                'to_addr': self.to_addr or '',
                'sent_at': str(self.sent_at or '')[:16],
                'excerpt': (self.body_excerpt or '')[:300],
                'suggested_lead_id': self.suggested_lead_id,
                'why': self.why or '',
                'status': self.status,
                'linked_lead_id': self.linked_lead_id,
                'created_at': str(self.created_at or '')[:16]}
