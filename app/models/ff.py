"""
Freight forwarding: asking several carriers, and comparing what
comes back.

FF-COMMS-01, R1.

`RateSourcingLine` already describes a leg that needs a rate, and it
carries exactly one — `rate_amount`, `rate_currency`, and a
free-text `vendor_source`. That is enough to record the rate you
went with and nothing else. A forwarder asks four agents, two reply,
one is cheaper but slower, and the choice has to be explainable in
March. None of that fits in one column.

So a `RateRequest` is one row per **line × vendor**: the ask and the
reply together, because they are the same event seen twice and
splitting them across two tables means a request with no reply has
nowhere to live — which is the case worth reporting, not the case
worth dropping.
"""
from datetime import datetime

from app import db

#: Where a request stands. `No Response` is set by a person or a
#: sweep, never inferred: silence and "not yet asked" look identical
#: in a database and mean opposite things to whoever is chasing.
REQUESTED = 'Requested'
REPLIED = 'Replied'
DECLINED = 'Declined'
NO_RESPONSE = 'No Response'
CANCELLED = 'Cancelled'
STATUSES = (REQUESTED, REPLIED, DECLINED, NO_RESPONSE, CANCELLED)
OPEN_STATUSES = (REQUESTED,)

#: Customs can be handled in-house at two ports and through a partner
#: everywhere else. Recorded on the line because it changes who is
#: asked for a rate, not on the quote.
CHA_OWN = 'Own'
CHA_PARTNER = 'Partner'
CHA_MODES = (CHA_OWN, CHA_PARTNER)

#: The two ports where Procam holds its own customs licence. Anywhere
#: else is a partner, and a line that says otherwise is a mistake the
#: service refuses rather than stores.
OWN_CHA_PORTS = ('Chennai', 'Mumbai')


class RateRequest(db.Model):
    """One carrier or agent, asked for one leg."""

    __tablename__ = 'ff_rate_requests'

    id = db.Column(db.Integer, primary_key=True)

    rfq_id = db.Column(db.Integer, db.ForeignKey('rfqs.id'),
                       nullable=False, index=True)
    #: The leg this is a rate for.
    line_id = db.Column(db.Integer,
                        db.ForeignKey('rate_sourcing_lines.id',
                                      ondelete='CASCADE'),
                        nullable=False, index=True)

    #: The vendor, by account where we have one and by name where we
    #: do not. Both are kept: an agent named in a hurry today is
    #: still the agent who quoted, and linking it later must not
    #: rewrite what the person typed.
    vendor_company_id = db.Column(db.Integer,
                                  db.ForeignKey('companies.id'),
                                  nullable=True, index=True)
    vendor_name = db.Column(db.String(240), nullable=False)
    vendor_email = db.Column(db.String(320))

    requested_at = db.Column(db.DateTime, default=datetime.utcnow,
                             index=True)
    requested_by = db.Column(db.String(20), index=True)
    due_by = db.Column(db.Date)
    request_note = db.Column(db.Text)

    status = db.Column(db.String(16), default=REQUESTED, nullable=False,
                       index=True)

    # ── what came back ───────────────────────────────────────────────
    replied_at = db.Column(db.DateTime)
    amount = db.Column(db.Numeric(15, 2))
    currency = db.Column(db.String(6), default='INR')
    #: The amount in rupees at the rate of the day it was recorded.
    #: Stored rather than computed on read: a comparison re-run next
    #: month must show the choice as it was made, not as today's FX
    #: would have made it.
    amount_inr = db.Column(db.Numeric(15, 2))
    validity_until = db.Column(db.Date)
    transit_days = db.Column(db.Integer)
    reply_remarks = db.Column(db.Text)
    attachment_path = db.Column(db.Text)

    # ── the choice ───────────────────────────────────────────────────
    chosen = db.Column(db.Boolean, default=False, nullable=False,
                       index=True)
    chosen_at = db.Column(db.DateTime)
    chosen_by = db.Column(db.String(20))
    #: Why this one. Required when it is not the cheapest, because
    #: that is the decision somebody will be asked about.
    chosen_reason = db.Column(db.String(300))

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    __table_args__ = (
        db.Index('ix_ff_rate_req_line_status', 'line_id', 'status'),
    )

    @property
    def is_open(self):
        return self.status in OPEN_STATUSES

    @property
    def waited_days(self):
        if self.replied_at or not self.requested_at:
            return None
        return (datetime.utcnow() - self.requested_at).days

    def to_dict(self):
        return {
            'id': self.id,
            'rfq_id': self.rfq_id,
            'line_id': self.line_id,
            'vendor_company_id': self.vendor_company_id,
            'vendor_name': self.vendor_name,
            'vendor_email': self.vendor_email or '',
            'requested_at': (str(self.requested_at)[:19]
                             if self.requested_at else ''),
            'requested_by': self.requested_by or '',
            'due_by': str(self.due_by) if self.due_by else '',
            'request_note': self.request_note or '',
            'status': self.status,
            'replied_at': (str(self.replied_at)[:19]
                           if self.replied_at else ''),
            'amount': float(self.amount) if self.amount is not None else None,
            'currency': self.currency or 'INR',
            'amount_inr': (float(self.amount_inr)
                           if self.amount_inr is not None else None),
            'validity_until': (str(self.validity_until)
                               if self.validity_until else ''),
            'transit_days': self.transit_days,
            'reply_remarks': self.reply_remarks or '',
            'chosen': bool(self.chosen),
            'chosen_at': str(self.chosen_at)[:19] if self.chosen_at else '',
            'chosen_by': self.chosen_by or '',
            'chosen_reason': self.chosen_reason or '',
            'waited_days': self.waited_days,
        }

    def __repr__(self):
        return (f'<RateRequest line={self.line_id} '
                f'{self.vendor_name!r} [{self.status}]>')


class CrnSequence(db.Model):
    """The counter behind `<BU>-<C|T><FY>-<NNNNNN>`.

    One row per business unit, series and financial year. The unique
    constraint is what makes two simultaneous enquiries queue rather
    than collide: without it both read the same last number and both
    write the next one.
    """

    __tablename__ = 'ff_crn_sequences'

    id = db.Column(db.Integer, primary_key=True)
    bu = db.Column(db.String(5), nullable=False)
    #: C for the customer side, T for the vendor side.
    kind = db.Column(db.String(1), nullable=False)
    #: Two digits, India's April-to-March year.
    fy = db.Column(db.String(2), nullable=False)
    last_serial = db.Column(db.Integer, nullable=False, default=0)

    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('bu', 'kind', 'fy', name='uq_ff_crn_series'),
    )

    def __repr__(self):
        return (f'<CrnSequence {self.bu}-{self.kind}{self.fy} '
                f'at {self.last_serial}>')


#: Who an email is for, and therefore whether the client may be
#: named in it. `vendor` is the one that matters: those go through
#: the redaction and the leak check in app/services/ff_privacy.py.
AUDIENCE_VENDOR = 'vendor'
AUDIENCE_CUSTOMER = 'customer'
AUDIENCE_INTERNAL = 'internal'
AUDIENCES = (AUDIENCE_VENDOR, AUDIENCE_CUSTOMER, AUDIENCE_INTERNAL)


class FfEmailTemplate(db.Model):
    """A freight-forwarding email, editable by an administrator.

    In the database rather than in the repository because the wording
    is the business's, not the code's: a sentence that reads wrong to
    a carrier should be fixable by the person who noticed, on the day
    they noticed, without a deploy.

    `audience` is not decoration. A template marked `vendor` is
    rendered through the redaction and refused if the client's name
    survives; one marked `customer` is not. Mislabel it and either a
    client gets a letter addressed to nobody, or a vendor learns who
    the client is.
    """

    __tablename__ = 'ff_email_templates'

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(16), unique=True, nullable=False,
                     index=True)
    name = db.Column(db.String(120), nullable=False)
    audience = db.Column(db.String(12), nullable=False,
                         default=AUDIENCE_INTERNAL)

    subject = db.Column(db.String(300), nullable=False)
    body_html = db.Column(db.Text, nullable=False)
    body_text = db.Column(db.Text)

    #: What the body may refer to, listed for whoever edits it. Not
    #: enforced: a template that mentions an unknown field renders it
    #: blank, which is better than refusing to send the mail.
    placeholders = db.Column(db.JSON, default=list)

    is_active = db.Column(db.Boolean, nullable=False, default=True)
    updated_by = db.Column(db.String(20))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'code': self.code, 'name': self.name,
            'audience': self.audience, 'subject': self.subject,
            'body_html': self.body_html or '',
            'body_text': self.body_text or '',
            'placeholders': list(self.placeholders or []),
            'is_active': bool(self.is_active),
            'updated_by': self.updated_by or '',
            'updated_at': (str(self.updated_at)[:19]
                           if self.updated_at else ''),
        }

    def __repr__(self):
        return f'<FfEmailTemplate {self.code} ({self.audience})>'


class FfHandoverPack(db.Model):
    """What operations is given when a forwarding job is confirmed.

    FF-COMMS-01 R4. The customer's confirmation email *is* the
    purchase order on a forwarding job — there is rarely a PO
    document — so the message itself is captured: who sent it, when,
    its subject and its Message-Id. "The client confirmed" without
    those four is hearsay, and it is the thing argued about when a
    shipment goes wrong.

    The pack holds both artefacts: the Excel file operations reads,
    and the payload the TMS would be sent. The payload is *stored,
    not sent*. Nothing here calls the production TMS.
    """

    __tablename__ = 'ff_handover_packs'

    id = db.Column(db.Integer, primary_key=True)
    quote_id = db.Column(db.Integer, db.ForeignKey('quotes.id'),
                         nullable=False, index=True)
    crn = db.Column(db.String(24), index=True)

    # ── the confirmation, which is the PO ────────────────────────────
    confirmed_at = db.Column(db.DateTime)
    confirmed_by_email = db.Column(db.String(320))
    confirmation_subject = db.Column(db.String(400))
    #: RFC-5322 Message-Id. The one identifier that survives
    #: forwarding, and the only way to point at the exact mail later.
    confirmation_message_id = db.Column(db.String(400), index=True)
    confirmation_note = db.Column(db.Text)
    #: The customer's own purchase-order number, where they issue
    #: one. The TMS keeps it beside Procam's number and never uses
    #: it as one: `tally_po_number` on a Handover Pack,
    #: `client_po_number` on a typed order. On a forwarding job
    #: there is usually no PO document at all, which is why the
    #: confirmation email above is captured instead.
    client_po_number = db.Column(db.String(60))

    #: Draft / Ready / Delivered. `Delivered` is never set by this
    #: release: nothing sends the pack anywhere yet.
    status = db.Column(db.String(16), default='Draft', nullable=False,
                       index=True)

    #: The TMS payload, as it would be sent. Kept so it can be read,
    #: diffed and approved before anything is wired to the live
    #: system.
    payload_json = db.Column(db.Text)
    file_path = db.Column(db.Text)

    built_at = db.Column(db.DateTime)
    built_by = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    def to_dict(self):
        import json
        payload = None
        if self.payload_json:
            try:
                payload = json.loads(self.payload_json)
            except Exception:                             # noqa: BLE001
                payload = None
        return {
            'id': self.id, 'quote_id': self.quote_id,
            'crn': self.crn or '', 'status': self.status,
            'confirmed_at': (str(self.confirmed_at)[:19]
                             if self.confirmed_at else ''),
            'confirmed_by_email': self.confirmed_by_email or '',
            'confirmation_subject': self.confirmation_subject or '',
            'confirmation_message_id': self.confirmation_message_id or '',
            'confirmation_note': self.confirmation_note or '',
            'file_path': self.file_path or '',
            'built_at': str(self.built_at)[:19] if self.built_at else '',
            'built_by': self.built_by or '',
            'payload': payload,
        }

    def __repr__(self):
        return f'<FfHandoverPack quote={self.quote_id} {self.status}>'
