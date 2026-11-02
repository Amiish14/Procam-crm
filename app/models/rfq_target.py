"""
How many RFQs a person is meant to bring in, per month.

CRM-LEAD-JOURNEY-01 §6.3 (D8).

A target per month, per vertical, per salesperson. Deliberately
not a target per lead or per channel: the sheet the sales team
already works to counts RFQs, and a target nobody recognises is
one nobody works to.

PROCAM-DECISIONS-02NOV Module 3 adds two things to the same table
rather than a second one beside it:

  1. where the number came from (`source` and `basis`), because
     Module 3.1 pre-fills it from the last four months and "was 7
     a month plus 20%" is the first question anybody asks of a
     target that looks wrong;
  2. `RfqTargetHistory`, because 3.2 says "Admin can edit any
     target later on one screen, with history kept".

History is a second table and not a `superseded_at` column, because
a target is a number that moves repeatedly through a year — unlike
a term set, which is a document republished rarely. Thirty versions
of a document is a library; thirty versions of a number is a log.
"""
from datetime import datetime

from app import db


class RfqTarget(db.Model):
    __tablename__ = 'rfq_targets'
    __table_args__ = (
        db.UniqueConstraint('month', 'vertical', 'emp_code',
                            name='uq_rfq_target'),
    )

    id = db.Column(db.Integer, primary_key=True)
    #: 'YYYY-MM'. A string because every query against it is a
    #: prefix or an equality, and a date would invite a month
    #: boundary bug for no gain.
    month = db.Column(db.String(7), nullable=False, index=True)
    vertical = db.Column(db.String(60), nullable=False, index=True)
    emp_code = db.Column(db.String(20), nullable=False, index=True)
    target_rfqs = db.Column(db.Integer, nullable=False, default=0)

    #: 'seeded' (the CRM's reading of the last four months) or
    #: 'manual' (a person decided). Nullable, because every row
    #: written before this column existed was set by hand on the
    #: journey screen and calling those 'seeded' would be a claim
    #: about where they came from that nobody made.
    source = db.Column(db.String(10), nullable=True)
    #: What the seed read, kept beside the target even after
    #: somebody edits it.
    basis = db.Column(db.String(200), nullable=True)

    updated_by = db.Column(db.String(20))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    def to_dict(self):
        return {'id': self.id, 'month': self.month,
                'vertical': self.vertical, 'emp_code': self.emp_code,
                'target_rfqs': self.target_rfqs or 0,
                'source': self.source or '',
                'basis': self.basis or '',
                'updated_by': self.updated_by or '',
                'updated_at': (str(self.updated_at)[:19]
                               if self.updated_at else '')}


#: How the number got there.
SEEDED = 'seeded'
MANUAL = 'manual'


class RfqTargetHistory(db.Model):
    """Every change to a target, including the one that set it.

    The first write is logged too. Without it the first edit looks
    like the only decision anybody made, and "who set this" has no
    answer for the original value.
    """

    __tablename__ = 'rfq_target_history'
    __table_args__ = (
        db.Index('ix_rfq_target_history_row', 'target_id',
                 'created_at'),
    )

    id = db.Column(db.Integer, primary_key=True)
    target_id = db.Column(db.Integer, nullable=False, index=True)
    #: Repeated from the target so a history row reads on its own: a
    #: target deleted by some future migration should not take the
    #: record of who changed it along with it.
    emp_code = db.Column(db.String(20), nullable=False, index=True)
    vertical = db.Column(db.String(60), nullable=False)
    month = db.Column(db.String(7), nullable=False, index=True)
    old_target = db.Column(db.Integer, nullable=True)
    new_target = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(300))
    actor = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           index=True)

    def to_dict(self):
        return {
            'id': self.id, 'emp_code': self.emp_code,
            'vertical': self.vertical, 'month': self.month,
            'old_target': self.old_target,
            'new_target': self.new_target,
            'reason': self.reason or '', 'actor': self.actor or '',
            'at': str(self.created_at)[:19] if self.created_at else '',
        }
