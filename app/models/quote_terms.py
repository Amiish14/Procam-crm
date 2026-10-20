"""
Standard terms and special conditions on a quote.

CRM 2.0 R5. Two tables, because they are two different things that
get confused constantly:

  QuoteTermSet         the standard terms (STC) a vertical quotes
                       under. One per vertical at a time, versioned,
                       never edited in place.
  QuoteSpecialCondition the conditions (SCC) added to one quote
                       because that job is unusual.

Why the STC is versioned rather than edited
    A quote printed in March was made under March's terms. Editing
    the row in June silently rewrites what the client agreed to, and
    the only copy of the old wording is in a PDF somebody emailed.
    So a change makes a new version and the quote holds the version
    number it was printed under — the question "what did they
    actually sign up to" has an answer in the database.
"""
from __future__ import annotations

from datetime import datetime

from app import db


class QuoteTermSet(db.Model):
    """One version of the standard terms for one vertical."""

    __tablename__ = 'quote_term_sets'

    id          = db.Column(db.Integer, primary_key=True)
    vertical    = db.Column(db.String(80), nullable=False, index=True)
    version     = db.Column(db.Integer, nullable=False, default=1)
    title       = db.Column(db.String(200), nullable=False,
                            default='Standard Terms and Conditions')
    body        = db.Column(db.Text, nullable=False)
    #: Only one version per vertical is current. Superseding is how
    #: a version stops being current, never deletion.
    is_current  = db.Column(db.Boolean, nullable=False, default=True,
                            index=True)
    effective_from = db.Column(db.Date, nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    created_by  = db.Column(db.String(20))
    superseded_at = db.Column(db.DateTime, nullable=True)
    superseded_by_id = db.Column(db.Integer, nullable=True)

    __table_args__ = (
        db.UniqueConstraint('vertical', 'version',
                            name='uq_term_set_version'),
        db.Index('ix_term_set_current', 'vertical', 'is_current'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'vertical': self.vertical,
            'version': self.version,
            'title': self.title,
            'body': self.body or '',
            'is_current': bool(self.is_current),
            'effective_from': (str(self.effective_from)
                               if self.effective_from else ''),
        }


class QuoteSpecialCondition(db.Model):
    """One condition that applies to one quote and not to others."""

    __tablename__ = 'quote_special_conditions'

    id          = db.Column(db.Integer, primary_key=True)
    quote_id    = db.Column(db.Integer,
                            db.ForeignKey('quotes.id'),
                            nullable=False, index=True)
    seq         = db.Column(db.Integer, nullable=False, default=1)
    text        = db.Column(db.Text, nullable=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    created_by  = db.Column(db.String(20))

    __table_args__ = (
        db.Index('ix_scc_quote_seq', 'quote_id', 'seq'),
    )

    def to_dict(self):
        return {'id': self.id, 'quote_id': self.quote_id,
                'seq': self.seq, 'text': self.text or ''}
