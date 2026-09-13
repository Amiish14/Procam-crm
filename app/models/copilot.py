"""Procam AI's audit trail — §11.

One row per question asked. It records what was asked, which intent
answered it, which sources were touched and how long it took; never the
model's reasoning, which §11 excludes, and never the answer's rows,
which would copy business data into a log with different retention.

It is also the adoption analytics: unanswered questions, feedback
scores, slow queries and access refusals all fall out of these columns.
"""
from datetime import datetime

from app import db


class CopilotLog(db.Model):
    __tablename__ = 'copilot_log'

    id            = db.Column(db.Integer, primary_key=True)
    emp_code      = db.Column(db.String(20), index=True)
    question      = db.Column(db.String(1000), nullable=False)
    #: Empty when nothing in the catalogue matched — the column that
    #: tells an admin what to build next.
    intent        = db.Column(db.String(60), index=True)
    data_scope    = db.Column(db.String(12))
    sources       = db.Column(db.String(500))
    answered      = db.Column(db.Boolean, default=False, index=True)
    #: The §6.6 safe form was returned instead of detail.
    restricted    = db.Column(db.Boolean, default=False)
    model_used    = db.Column(db.Boolean, default=False)
    latency_ms    = db.Column(db.Integer)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow,
                              index=True)

    # ── §6.4 feedback ────────────────────────────────────────────────
    helpful         = db.Column(db.Boolean, nullable=True, index=True)
    feedback_reason = db.Column(db.String(60))
    feedback_note   = db.Column(db.String(500))
    feedback_by     = db.Column(db.String(20))

    def to_dict(self):
        return {
            'id': self.id, 'emp_code': self.emp_code,
            'question': self.question, 'intent': self.intent or '',
            'data_scope': self.data_scope, 'sources': self.sources or '',
            'answered': bool(self.answered),
            'restricted': bool(self.restricted),
            'model_used': bool(self.model_used),
            'latency_ms': self.latency_ms or 0,
            'created_at': str(self.created_at)[:19],
            'helpful': self.helpful,
            'feedback_reason': self.feedback_reason or '',
        }
