"""
Clients the company has decided not to do business with, or to be
careful with.

Two tables. `client_restrictions` is the current state of one decision
— who it is about, why, who asked for it and who agreed. It is the row
the central check reads on every create path in the CRM, so it is kept
small and indexed on the things that get matched.

`client_restriction_events` is everything that has ever happened to
that decision, appended and never edited: who recommended it, who
approved it, every attempt somebody made to create business against it,
every acknowledgement of a caution, and the lift. Nothing here is ever
deleted — a block that is lifted keeps its whole history, because the
next person to ask "have we had trouble with these people?" needs the
answer even after the block has gone.

Why the lists are JSON text and not array columns: production is
SQLite. `aliases`, `domains`, `emails` and `business_units` are short
lists that are only ever read whole, so a JSON column costs nothing and
keeps the same shape if the database ever moves to PostgreSQL.
"""
import json
from datetime import datetime

from app import db

#: The states a decision can be in.
RECOMMENDED = 'recommended'
CAUTION = 'caution'
BLOCKED = 'blocked'
REJECTED = 'rejected'
LIFTED = 'lifted'
STATUSES = (RECOMMENDED, CAUTION, BLOCKED, REJECTED, LIFTED)

#: The states that actually stop or warn about business. A rejected
#: recommendation and a lifted block are history, not policy.
LIVE_STATUSES = (CAUTION, BLOCKED)

#: How wide a decision reaches.
SCOPE_ENTITY = 'entity'    # the company and its aliases
SCOPE_GROUP = 'group'      # everything on the domain, group companies too
SCOPES = (SCOPE_ENTITY, SCOPE_GROUP)

LEGAL_STATUSES = ('None', 'Notice sent', 'In arbitration', 'In court',
                  'Settled')

BUSINESS_UNITS = ('PLPL', 'PWLPL')


def _loads(raw):
    try:
        out = json.loads(raw or '[]')
        return [str(v) for v in out] if isinstance(out, list) else []
    except Exception:
        return []


class ClientRestriction(db.Model):
    __tablename__ = 'client_restrictions'

    id = db.Column(db.Integer, primary_key=True)
    status = db.Column(db.String(16), nullable=False, default=RECOMMENDED,
                       index=True)

    company_name = db.Column(db.String(240), nullable=False, index=True)
    #: The company name reduced to its comparable form — lower case, no
    #: punctuation, no Ltd/Limited/Pvt. Stored rather than computed on
    #: every check so the common case is an indexed equality test and
    #: the fuzzy pass only runs on what is left.
    name_key = db.Column(db.String(240), index=True)

    aliases_json = db.Column(db.Text)
    domains_json = db.Column(db.Text)
    emails_json = db.Column(db.Text)

    gstin = db.Column(db.String(20), index=True)
    pan = db.Column(db.String(15), index=True)
    linked_account_id = db.Column(db.Integer, index=True)

    reason_category = db.Column(db.String(60))
    reason_detail = db.Column(db.Text, nullable=False)

    dispute_amount = db.Column(db.Numeric(18, 2))
    currency = db.Column(db.String(6), default='INR')
    dispute_refs = db.Column(db.String(500))
    legal_status = db.Column(db.String(30), default='None')

    recommended_by = db.Column(db.String(20), index=True)
    recommended_at = db.Column(db.DateTime, default=datetime.utcnow)
    approved_by = db.Column(db.String(20))
    approved_at = db.Column(db.DateTime)

    scope = db.Column(db.String(10), default=SCOPE_ENTITY)
    business_units_json = db.Column(db.Text)

    review_date = db.Column(db.Date, index=True)
    #: Set once the review-date reminder has gone, so the daily sweep
    #: does not send it every morning after the date passes.
    review_reminded_at = db.Column(db.DateTime)

    lifted_by = db.Column(db.String(20))
    lifted_at = db.Column(db.DateTime)
    lift_reason = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    # ── the list fields, as lists ────────────────────────────────────
    @property
    def aliases(self):
        return _loads(self.aliases_json)

    @aliases.setter
    def aliases(self, values):
        self.aliases_json = json.dumps([v.strip() for v in (values or [])
                                        if str(v).strip()])

    @property
    def domains(self):
        return [d.lower() for d in _loads(self.domains_json)]

    @domains.setter
    def domains(self, values):
        clean = []
        for value in (values or []):
            dom = str(value).strip().lower().lstrip('@')
            if dom.startswith('http'):
                dom = dom.split('//', 1)[-1]
            dom = dom.split('/', 1)[0]
            if dom and dom not in clean:
                clean.append(dom)
        self.domains_json = json.dumps(clean)

    @property
    def emails(self):
        return [e.lower() for e in _loads(self.emails_json)]

    @emails.setter
    def emails(self, values):
        self.emails_json = json.dumps(
            sorted({str(v).strip().lower() for v in (values or [])
                    if '@' in str(v)}))

    @property
    def business_units(self):
        found = _loads(self.business_units_json)
        return found or list(BUSINESS_UNITS)

    @business_units.setter
    def business_units(self, values):
        keep = [v for v in (values or []) if v in BUSINESS_UNITS]
        self.business_units_json = json.dumps(keep or list(BUSINESS_UNITS))

    # ── helpers ──────────────────────────────────────────────────────
    @property
    def is_live(self):
        return self.status in LIVE_STATUSES

    def covers_unit(self, business_unit):
        if not business_unit:
            return True
        return business_unit in self.business_units

    def to_dict(self, *, sensitive=False):
        """`sensitive` adds the dispute money and references, which are
        for administrators — everyone may see that a client is blocked
        and why, not what is owed."""
        out = {
            'id': self.id,
            'status': self.status,
            'company_name': self.company_name,
            'aliases': self.aliases,
            'domains': self.domains,
            'scope': self.scope,
            'business_units': self.business_units,
            'reason_category': self.reason_category or '',
            'reason_detail': self.reason_detail or '',
            'legal_status': self.legal_status or 'None',
            'recommended_by': self.recommended_by or '',
            'recommended_at': str(self.recommended_at)[:16]
                              if self.recommended_at else '',
            'approved_by': self.approved_by or '',
            'approved_at': str(self.approved_at)[:16]
                           if self.approved_at else '',
            'review_date': str(self.review_date) if self.review_date else '',
            'lifted_by': self.lifted_by or '',
            'lift_reason': self.lift_reason or '',
        }
        if sensitive:
            out.update({
                'emails': self.emails,
                'gstin': self.gstin or '',
                'pan': self.pan or '',
                'dispute_amount': (float(self.dispute_amount)
                                   if self.dispute_amount is not None else None),
                'currency': self.currency or 'INR',
                'dispute_refs': self.dispute_refs or '',
            })
        return out


class ClientRestrictionEvent(db.Model):
    """Append-only. One row per thing that happened."""
    __tablename__ = 'client_restriction_events'

    id = db.Column(db.Integer, primary_key=True)
    restriction_id = db.Column(db.Integer, index=True, nullable=False)
    #: recommended | approved | rejected | blocked | caution | lifted |
    #: edited | attempt_blocked | caution_acknowledged | records_closed
    action = db.Column(db.String(30), nullable=False, index=True)
    user_id = db.Column(db.String(20), index=True)
    note = db.Column(db.Text)
    payload_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    @property
    def payload(self):
        try:
            return json.loads(self.payload_json or '{}')
        except Exception:
            return {}

    def to_dict(self):
        return {
            'id': self.id,
            'action': self.action,
            'user_id': self.user_id or '',
            'note': self.note or '',
            'payload': self.payload,
            'at': str(self.created_at)[:16] if self.created_at else '',
        }


class RestrictionAttachment(db.Model):
    """A notice, an email, a legal letter."""
    __tablename__ = 'client_restriction_attachments'

    id = db.Column(db.Integer, primary_key=True)
    restriction_id = db.Column(db.Integer, index=True, nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    content_type = db.Column(db.String(120))
    size_bytes = db.Column(db.Integer)
    storage_path = db.Column(db.String(600), nullable=False)
    uploaded_by = db.Column(db.String(20))
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {'id': self.id, 'filename': self.filename,
                'size_bytes': self.size_bytes or 0,
                'uploaded_by': self.uploaded_by or '',
                'uploaded_at': str(self.uploaded_at)[:16]
                               if self.uploaded_at else ''}
