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
    #: §6.4 — a pinned answer. Stored as a flag on the question rather
    #: than a copy of the answer: re-running it gives today's numbers,
    #: and a saved table of last month's pipeline would quietly become
    #: wrong while looking authoritative.
    pinned          = db.Column(db.Boolean, default=False, index=True)
    pinned_at       = db.Column(db.DateTime)

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
            'pinned': bool(self.pinned),
            'helpful': self.helpful,
            'feedback_reason': self.feedback_reason or '',
        }


class CopilotChunk(db.Model):
    """§4 — one retrievable piece of unstructured CRM text.

    The permission metadata is on the row, not looked up at read time,
    so the scope filter is part of the query that selects candidates.
    That is what makes "filtering happens before retrieval" true rather
    than aspirational.

    It also means the row is only as correct as the last re-index —
    hence retrieval.invalidate_lead(), called whenever ownership or
    vertical changes.
    """
    __tablename__ = 'copilot_chunk'

    id            = db.Column(db.Integer, primary_key=True)
    lead_id       = db.Column(db.Integer, index=True, nullable=False)
    company_id    = db.Column(db.Integer, index=True)
    #: enquiry | note | email:inbound | email:outbound
    source        = db.Column(db.String(24))
    seq           = db.Column(db.Integer, default=0)

    # ── the permission metadata the scope filter matches on ──────────
    owner_emp_code     = db.Column(db.String(20), index=True)
    secondary_emp_code = db.Column(db.String(20), index=True)
    vertical           = db.Column(db.String(60), index=True)

    account_name  = db.Column(db.String(240))
    occurred_at   = db.Column(db.DateTime, index=True)
    text          = db.Column(db.Text, nullable=False)
    #: JSON array when an internal embedder has run; null until then,
    #: and the lexical backend answers in the meantime.
    embedding     = db.Column(db.Text)
    indexed_at    = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {'id': self.id, 'lead_id': self.lead_id,
                'source': self.source, 'account': self.account_name or '',
                'vertical': self.vertical or '',
                'when': str(self.occurred_at or '')[:10],
                'text': self.text}
