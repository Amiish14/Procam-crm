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
