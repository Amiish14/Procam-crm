"""
RFQ + Rate Sourcing — data model (Phase 6 of the CRM upgrade).

Three tables:

    rfqs                — a request-for-quote from a customer, tied to a
                          Lead / Opportunity / Account / Project.
    rfq_attachments     — customer-supplied files attached to the RFQ.
    rate_sourcing_lines — one row per service line the RFQ is soliciting
                          rates for.  Each line carries its own owner
                          (typically a Rate_Sourcing role member) plus the
                          rate returned by that owner.

Per-service owner is captured on `RateSourcingLine.sourcing_owner_id`.
We deliberately do NOT add PIC1/PIC2/PIC3 columns on the RFQ header —
the shared assignment engine (`app.services.assignment`) is the single
source of truth for who owns the account/deal.
"""
from datetime import date, datetime

from app import db


class RFQ(db.Model):
    __tablename__ = 'rfqs'

    id             = db.Column(db.Integer, primary_key=True)
    rfq_number     = db.Column(db.String(40), unique=True, nullable=False, index=True)

    lead_id        = db.Column(db.Integer, db.ForeignKey('leads.id'),
                               nullable=True, index=True)
    opportunity_id = db.Column(db.Integer, db.ForeignKey('opportunities.id'),
                               nullable=True, index=True)
    account_id     = db.Column(db.Integer, db.ForeignKey('companies.id'),
                               nullable=True, index=True)
    project_id     = db.Column(db.Integer, db.ForeignKey('projects.id'),
                               nullable=True, index=True)

    #: FF-COMMS-01 R2. `crn` is the customer-side reference carried
    #: from the enquiry; `vendor_crn` is the T-series number, and is
    #: the only reference that ever reaches a carrier or agent.
    crn            = db.Column(db.String(24), index=True)
    vendor_crn     = db.Column(db.String(24), unique=True, index=True)

    subject        = db.Column(db.String(240), nullable=False)
    description    = db.Column(db.Text)

    origin         = db.Column(db.String(120))
    destination    = db.Column(db.String(120))
    cargo          = db.Column(db.String(240))

    # ── the shipment, as the TMS intake format asks for it ───────
    # docs/tms_import_formats.md Part A. These were the fields the
    # handover pack had to leave blank for a person to fill in at
    # the far end, which meant typing them twice and getting them
    # from memory the second time. Captured once, on the enquiry,
    # and read by the rate request, the quote and the pack.
    direction      = db.Column(db.String(12))   # EXPORT/IMPORT/CROSS_TRADE
    shipper_name     = db.Column(db.String(200))
    shipper_address  = db.Column(db.Text)
    consignee_name    = db.Column(db.String(200))
    consignee_address = db.Column(db.Text)
    #: UN/LOCODE for sea, IATA for air. Not validated here: the TMS
    #: does not validate them either, and a code it would accept
    #: that we reject is a field somebody works around.
    pol_code       = db.Column(db.String(10))
    pol_name       = db.Column(db.String(120))
    pod_code       = db.Column(db.String(10))
    pod_name       = db.Column(db.String(120))
    hs_code        = db.Column(db.String(20))
    package_count  = db.Column(db.Integer)
    package_kind   = db.Column(db.String(60))
    gross_weight_kg = db.Column(db.Numeric(14, 3))
    volume_cbm     = db.Column(db.Numeric(12, 3))
    container_count = db.Column(db.Integer)
    #: Tri-state on purpose. Null is "nobody has said", False is "a
    #: person said no". The TMS reads truthiness, so only a True
    #: ever reaches the pack — but the difference matters here,
    #: because an unanswered DG question is not the same as a no.
    is_dangerous   = db.Column(db.Boolean)
    dg_class       = db.Column(db.String(20))
    un_number      = db.Column(db.String(12))
    scope          = db.Column(db.Text)

    received_date  = db.Column(db.Date, default=date.today, nullable=False)
    close_date     = db.Column(db.Date, nullable=True)
    quote_by_date  = db.Column(db.Date, nullable=True)
    currency       = db.Column(db.String(6), default='INR')
    lead_driver    = db.Column(db.String(20))   # emp_code

    # Received / Rate Sourcing / Quote Preparation / Quoted /
    # Negotiation / Won / Lost / Withdrawn
    status         = db.Column(db.String(24), default='Received',
                               index=True, nullable=False)

    created_by_id  = db.Column(db.String(20))
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at     = db.Column(db.DateTime, default=datetime.utcnow,
                               onupdate=datetime.utcnow)

    def to_dict(self, *, deep=False):
        d = {
            'id':             self.id,
            'rfq_number':     self.rfq_number,
            'crn':            self.crn or '',
            'vendor_crn':     self.vendor_crn or '',
            'lead_id':        self.lead_id,
            'opportunity_id': self.opportunity_id,
            'account_id':     self.account_id,
            'project_id':     self.project_id,
            'subject':        self.subject or '',
            'description':    self.description or '',
            'origin':         self.origin or '',
            'destination':    self.destination or '',
            'cargo':          self.cargo or '',
            'scope':          self.scope or '',
            'received_date':  str(self.received_date) if self.received_date else '',
            'close_date':     str(self.close_date) if self.close_date else '',
            'quote_by_date':  str(self.quote_by_date) if self.quote_by_date else '',
            'currency':       self.currency or 'INR',
            'lead_driver':    self.lead_driver or '',
            'status':         self.status,
            'created_by_id':  self.created_by_id or '',
            'created_at':     str(self.created_at)[:19] if self.created_at else '',
            'updated_at':     str(self.updated_at)[:19] if self.updated_at else '',
        }
        if deep:
            d['lines'] = [ln.to_dict() for ln in RateSourcingLine.query
                          .filter_by(rfq_id=self.id)
                          .order_by(RateSourcingLine.line_no.asc(),
                                    RateSourcingLine.id.asc()).all()]
            d['attachments'] = [a.to_dict() for a in RFQAttachment.query
                                .filter_by(rfq_id=self.id)
                                .order_by(RFQAttachment.uploaded_at.desc()).all()]
        return d


class RFQAttachment(db.Model):
    __tablename__ = 'rfq_attachments'

    id             = db.Column(db.Integer, primary_key=True)
    rfq_id         = db.Column(db.Integer,
                               db.ForeignKey('rfqs.id', ondelete='CASCADE'),
                               nullable=False, index=True)
    filename       = db.Column(db.String(240))
    stored_path    = db.Column(db.Text)
    content_type   = db.Column(db.String(80))
    uploaded_at    = db.Column(db.DateTime, default=datetime.utcnow)
    uploaded_by_id = db.Column(db.String(20))

    def to_dict(self):
        return {
            'id':             self.id,
            'rfq_id':         self.rfq_id,
            'filename':       self.filename or '',
            'content_type':   self.content_type or '',
            'uploaded_at':    str(self.uploaded_at)[:19] if self.uploaded_at else '',
            'uploaded_by_id': self.uploaded_by_id or '',
        }


class RateSourcingLine(db.Model):
    __tablename__ = 'rate_sourcing_lines'

    id                  = db.Column(db.Integer, primary_key=True)
    rfq_id              = db.Column(db.Integer,
                                    db.ForeignKey('rfqs.id',
                                                  ondelete='CASCADE'),
                                    nullable=False, index=True)
    line_no             = db.Column(db.Integer)

    # Ocean, Air, Transport, Customs, Warehouse, Heavy Lift, Rigging, Other
    service             = db.Column(db.String(80), nullable=False)
    scope               = db.Column(db.Text)
    origin              = db.Column(db.String(120))
    destination         = db.Column(db.String(120))
    cargo               = db.Column(db.String(240))
    required_by         = db.Column(db.Date)

    sourcing_owner_id   = db.Column(db.String(20))          # emp_code
    supporting_user_ids = db.Column(db.JSON, default=list)
    vendor_source       = db.Column(db.String(240))
    requested_at        = db.Column(db.DateTime, default=datetime.utcnow)

    # Not Started / Requested / Awaiting Response / Partially Received / Completed
    status              = db.Column(db.String(24), default='Not Started',
                                    index=True, nullable=False)

    rate_received_at    = db.Column(db.DateTime)
    rate_amount         = db.Column(db.Numeric(15, 2))
    rate_currency       = db.Column(db.String(6))
    rate_validity_until = db.Column(db.Date)

    remarks             = db.Column(db.Text)
    attachment_path     = db.Column(db.Text)

    # FF-COMMS-01 R1.
    #: Own CHA at Chennai and Mumbai, a partner everywhere else. It
    #: changes who gets asked for a rate, so it belongs to the leg
    #: and not to the quote.
    cha_mode            = db.Column(db.String(12))
    #: Which bucket the sell line built from this leg belongs to.
    charge_group        = db.Column(db.String(24), index=True)

    created_by_id       = db.Column(db.String(20))
    created_at          = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at          = db.Column(db.DateTime, default=datetime.utcnow,
                                    onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id':                  self.id,
            'rfq_id':              self.rfq_id,
            'line_no':             self.line_no,
            'service':             self.service,
            'scope':               self.scope or '',
            'origin':              self.origin or '',
            'destination':         self.destination or '',
            'cargo':               self.cargo or '',
            'required_by':         str(self.required_by) if self.required_by else '',
            'sourcing_owner_id':   self.sourcing_owner_id or '',
            'supporting_user_ids': list(self.supporting_user_ids or []),
            'vendor_source':       self.vendor_source or '',
            'requested_at':        str(self.requested_at)[:19] if self.requested_at else '',
            'status':              self.status,
            'rate_received_at':    str(self.rate_received_at)[:19] if self.rate_received_at else '',
            'rate_amount':         float(self.rate_amount) if self.rate_amount is not None else None,
            'rate_currency':       self.rate_currency or '',
            'rate_validity_until': str(self.rate_validity_until) if self.rate_validity_until else '',
            'remarks':             self.remarks or '',
            'cha_mode':            self.cha_mode or '',
            'charge_group':        self.charge_group or '',
            'created_by_id':       self.created_by_id or '',
            'created_at':          str(self.created_at)[:19] if self.created_at else '',
            'updated_at':          str(self.updated_at)[:19] if self.updated_at else '',
        }
