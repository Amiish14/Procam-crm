"""
Who a lead belongs to, and how the CRM learns it.

Three tables, each replacing something that was previously a guess or
a gap:

    sender_account_link     what the CRM has learnt from corrections.
                            When somebody links a lead to an account
                            or reassigns it, the sender that produced
                            it is remembered, so the next mail from
                            the same person matches without anybody
                            doing anything.

    account_vertical_owner  an account can be served by different
                            people for different work. Godrej's
                            warehousing and Godrej's heavy transport
                            are not the same salesperson, and one
                            `pic_emp_code` on the account cannot say
                            so.

    owner_replacement       where a departed person's leads go. Held
                            as rows rather than in code, because the
                            list changes whenever somebody leaves and
                            a deploy is the wrong unit for that.

    owner_confirmation      a lead assigned by rule rather than by a
                            person, waiting for its head to confirm
                            or reassign. Drives the morning digest,
                            the 48-hour escalation and the Workbench
                            tile.
"""
from datetime import datetime

from app import db

#: How a sender was linked to an account.
BY_EMAIL = 'email'
BY_DOMAIN = 'domain'


class SenderAccountLink(db.Model):
    """A sender the CRM has learnt belongs to an account.

    Written when a person corrects the CRM — links a lead to an
    account, or reassigns one — not when the CRM guesses. A guess
    that teaches itself is a guess that compounds.
    """
    __tablename__ = 'sender_account_link'

    id = db.Column(db.Integer, primary_key=True)
    #: 'email' for one person, 'domain' for everybody at a company.
    kind = db.Column(db.String(10), nullable=False, index=True)
    #: The address or the domain, lower-cased.
    value = db.Column(db.String(320), nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey('companies.id'),
                           nullable=False, index=True)

    #: Which lead taught us, and who corrected it.
    learned_from_lead = db.Column(db.Integer)
    learned_by = db.Column(db.String(20))
    reason = db.Column(db.String(200))

    #: How often it has been used since. A link that never matches
    #: anything is one somebody can remove without worrying.
    hits = db.Column(db.Integer, default=0)
    last_hit_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        db.UniqueConstraint('kind', 'value', name='uq_sender_link'),
    )

    def to_dict(self):
        return {'id': self.id, 'kind': self.kind, 'value': self.value,
                'account_id': self.account_id, 'hits': self.hits or 0,
                'learned_by': self.learned_by or '',
                'learned_from_lead': self.learned_from_lead,
                'reason': self.reason or '',
                'created_at': str(self.created_at)[:16]
                if self.created_at else ''}


class AccountVerticalOwner(db.Model):
    """Who serves this account for this kind of work."""
    __tablename__ = 'account_vertical_owner'

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('companies.id'),
                           nullable=False, index=True)
    vertical = db.Column(db.String(60), nullable=False, index=True)
    emp_code = db.Column(db.String(20), nullable=False, index=True)

    #: 'seeded' when derived from the account's own history, 'manual'
    #: when a person set it. A seeded row is a reasonable guess from
    #: what has been happening; a manual one is a decision.
    source = db.Column(db.String(12), default='manual')
    note = db.Column(db.String(200))
    updated_by = db.Column(db.String(20))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('account_id', 'vertical',
                            name='uq_account_vertical'),
    )

    def to_dict(self):
        return {'id': self.id, 'account_id': self.account_id,
                'vertical': self.vertical, 'emp_code': self.emp_code,
                'source': self.source or 'manual', 'note': self.note or '',
                'updated_by': self.updated_by or ''}


class OwnerReplacement(db.Model):
    """Where a departed person's work goes."""
    __tablename__ = 'owner_replacement'

    id = db.Column(db.Integer, primary_key=True)
    from_emp_code = db.Column(db.String(20), unique=True, nullable=False,
                              index=True)
    to_emp_code = db.Column(db.String(20), nullable=False, index=True)
    note = db.Column(db.String(200))
    active = db.Column(db.Boolean, default=True, index=True)
    updated_by = db.Column(db.String(20))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    def to_dict(self):
        return {'id': self.id, 'from': self.from_emp_code,
                'to': self.to_emp_code, 'note': self.note or '',
                'active': bool(self.active),
                'updated_by': self.updated_by or ''}


class OwnerConfirmation(db.Model):
    """A lead a rule assigned, waiting for a person to agree.

    The lead has a real owner from the moment it is created — never a
    placeholder — and this row only records that nobody has yet said
    the owner is right. So a confirmation that is never answered
    costs nothing but a reminder.
    """
    __tablename__ = 'owner_confirmation'

    id = db.Column(db.Integer, primary_key=True)
    lead_id = db.Column(db.Integer, unique=True, nullable=False, index=True)
    #: The person the rule chose, and the rule that chose them.
    emp_code = db.Column(db.String(20), nullable=False, index=True)
    rule = db.Column(db.String(40))
    vertical = db.Column(db.String(60))

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    #: When the head was last told. Null until the first digest.
    notified_at = db.Column(db.DateTime)
    #: When somebody answered, and how.
    confirmed_at = db.Column(db.DateTime)
    confirmed_by = db.Column(db.String(20))
    outcome = db.Column(db.String(16))        # confirmed | reassigned
    reassigned_to = db.Column(db.String(20))
    #: When it was escalated for going unanswered.
    escalated_at = db.Column(db.DateTime)

    def to_dict(self):
        return {'id': self.id, 'lead_id': self.lead_id,
                'emp_code': self.emp_code, 'rule': self.rule or '',
                'vertical': self.vertical or '',
                'created_at': str(self.created_at)[:16]
                if self.created_at else '',
                'confirmed_at': str(self.confirmed_at)[:16]
                if self.confirmed_at else '',
                'outcome': self.outcome or '',
                'escalated': bool(self.escalated_at)}
