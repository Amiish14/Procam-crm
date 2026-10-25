"""
How many RFQs a person is meant to bring in, per month.

CRM-LEAD-JOURNEY-01 §6.3 (D8).

A target per month, per vertical, per salesperson. Deliberately
not a target per lead or per channel: the sheet the sales team
already works to counts RFQs, and a target nobody recognises is
one nobody works to.
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

    updated_by = db.Column(db.String(20))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    def to_dict(self):
        return {'id': self.id, 'month': self.month,
                'vertical': self.vertical, 'emp_code': self.emp_code,
                'target_rfqs': self.target_rfqs or 0}
