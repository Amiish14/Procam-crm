"""
What an escalation sweep has already sent — Group C.

The sweep runs every fifteen minutes. Without a record of what it sent,
an RFQ one hour past its deadline would reach its owner ninety-six times
a day, and the owner would turn the notifications off — which is the
only way an escalation system can truly fail.

One row per (record, level, recipient), written only once the message
was actually accepted. A delivery that failed writes nothing, so the
next sweep tries again; a delivery that succeeded can never be repeated,
because `dedupe_key` is unique.
"""
from datetime import datetime

from app import db


class EscalationLog(db.Model):
    __tablename__ = 'escalation_log'

    id = db.Column(db.Integer, primary_key=True)

    #: 'rfq' today. The column exists so a second chain (a quote past
    #: its validity, a handover awaiting a PO) costs a row, not a table.
    entity_type = db.Column(db.String(24), nullable=False, index=True)
    entity_id = db.Column(db.Integer, nullable=False, index=True)

    #: The level that fired — one of escalation.STAGE_KEYS.
    stage = db.Column(db.String(40), nullable=False, index=True)
    #: Who it went to (emp_code).
    recipient = db.Column(db.String(20), nullable=False)

    #: entity_type:entity_id:stage:recipient — the whole idempotency
    #: guarantee, enforced by the database rather than by a query that
    #: two sweeps could race through together.
    dedupe_key = db.Column(db.String(160), nullable=False, unique=True,
                           index=True)

    #: When the level became due, as against when it was sent. The gap
    #: is how late the sweep itself was, which is worth being able to see.
    due_at = db.Column(db.DateTime, nullable=True)
    sent_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    #: 'notification' or 'notification+email'.
    channel = db.Column(db.String(32), nullable=True)
    #: Suppressed by the notifier's own duplicate window, rather than
    #: freshly written. Still delivered, so still never repeated here.
    suppressed = db.Column(db.Boolean, nullable=False, default=False)
    note = db.Column(db.String(300), nullable=True)

    __table_args__ = (
        db.Index('ix_escalation_entity_stage',
                 'entity_type', 'entity_id', 'stage'),
    )

    def to_dict(self):
        return {
            'id': self.id, 'entity_type': self.entity_type,
            'entity_id': self.entity_id, 'stage': self.stage,
            'recipient': self.recipient,
            'due_at': str(self.due_at)[:19] if self.due_at else '',
            'sent_at': str(self.sent_at)[:19] if self.sent_at else '',
            'channel': self.channel or '',
            'suppressed': bool(self.suppressed),
            'note': self.note or '',
        }

    def __repr__(self):
        return (f'<EscalationLog {self.entity_type}#{self.entity_id} '
                f'{self.stage} → {self.recipient}>')
