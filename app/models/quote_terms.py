"""
Standard terms and special conditions on a quote.

CRM 2.0 R5. Two tables, because they are two different things that
get confused constantly:

  QuoteTermSet         the standard terms (STC). Versioned, never
                       edited in place. Two kinds of row, see below.
  QuoteSpecialCondition the conditions (SCC) added to one quote
                       because that job is unusual.
  QuoteTermsDocument    the client's or the tender's own T&C, as a
                       file, either replacing the standard or added
                       to it.

Master and annexure (PROCAM-DECISIONS-02NOV Module 1)
    Until 2 November there was one term set per vertical and nothing
    shared, so the same payment and liability wording was repeated
    six times and could be six different things. The decision is one
    **Procam Standard T&C** master, attached by default to every
    quote in every vertical, with an optional per-vertical annexure
    for what genuinely differs — free time, surcharges, permit lead
    time, site readiness.

    Both live in this one table, told apart by `kind`:

      kind='master'     vertical is '' — there is one lineage of
                        these and every quote prints it
      kind='annexure'   vertical names one service, and the row is
                        printed after the master when the quote is
                        in that vertical

    One table rather than two because they are the same object in
    every respect that matters here: versioned, effective-dated,
    superseded rather than edited, and quoted by number on a printed
    quote. `kind` defaults to 'annexure', so the six rows written in
    October keep meaning exactly what they meant.

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


#: The two kinds of row in `quote_term_sets`.
MASTER = 'master'
ANNEXURE = 'annexure'

#: A master row belongs to no vertical. Empty rather than a word like
#: 'All', because 'All' is a value the vertical column has carried
#: before — it was an employee grouping that reached the vertical
#: master — and a sentinel that is also a real past value is a
#: sentinel that will match something one day.
MASTER_VERTICAL = ''


class QuoteTermSet(db.Model):
    """One version of a standard term set: the master, or an annexure."""

    __tablename__ = 'quote_term_sets'

    id          = db.Column(db.Integer, primary_key=True)
    #: '' for the master; the service name for an annexure.
    vertical    = db.Column(db.String(80), nullable=False, index=True)
    #: 'master' or 'annexure'. Defaults to annexure so that the rows
    #: written before this column existed keep their meaning.
    kind        = db.Column(db.String(12), nullable=False,
                            default=ANNEXURE, index=True)
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

    @property
    def is_master(self):
        return (self.kind or ANNEXURE) == MASTER

    def to_dict(self):
        return {
            'id': self.id,
            'vertical': self.vertical,
            'kind': self.kind or ANNEXURE,
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


class QuoteTermsDocument(db.Model):
    """The client's or the tender's own T&C, uploaded against a quote.

    Keyed on the **number**, not the quote row (Module 1.3: "stored
    against the RFQ/QUO number and all its revisions"). A revision
    makes a new quote row; the client's terms do not change because
    we re-priced, so a document attached to QUO-000123 is found by
    revision 2 and 3 without being re-uploaded — and without being
    copied, which is how two revisions end up disagreeing about what
    the client sent.

    `mode` is the person's answer to the only question the file
    raises:

      replace   their T&C governs; the Procam master is not printed
      add       both apply; the master prints, then theirs

    There is no default. A file dropped on a quote with nobody saying
    which of the two it is would be printed one way and argued the
    other.
    """

    __tablename__ = 'quote_terms_documents'

    REPLACE = 'replace'
    ADD = 'add'

    id            = db.Column(db.Integer, primary_key=True)
    #: 'QUO-000123' or 'RFQ-000456' — what this follows.
    doc_number    = db.Column(db.String(40), nullable=False, index=True)
    #: The quote row it was uploaded from, for the audit trail only.
    quote_id      = db.Column(db.Integer, nullable=True, index=True)
    filename      = db.Column(db.String(240))
    stored_path   = db.Column(db.Text)
    content_type  = db.Column(db.String(120))
    #: 'replace' or 'add'.
    mode          = db.Column(db.String(10), nullable=False)
    #: Free text: "GeM bid 2026/1187", "client's purchase T&C rev C".
    note          = db.Column(db.String(300))
    uploaded_at   = db.Column(db.DateTime, default=datetime.utcnow)
    uploaded_by   = db.Column(db.String(20))
    #: Withdrawn rather than deleted, so a quote that printed under
    #: it can still say what it printed under.
    withdrawn_at  = db.Column(db.DateTime, nullable=True)
    withdrawn_by  = db.Column(db.String(20))

    __table_args__ = (
        db.Index('ix_terms_doc_number_live', 'doc_number',
                 'withdrawn_at'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'doc_number': self.doc_number or '',
            'filename': self.filename or '',
            'content_type': self.content_type or '',
            'mode': self.mode or '',
            'note': self.note or '',
            'uploaded_at': (str(self.uploaded_at)[:19]
                            if self.uploaded_at else ''),
            'uploaded_by': self.uploaded_by or '',
            'withdrawn': self.withdrawn_at is not None,
        }


class AccountStandingCondition(db.Model):
    """A special condition that applies to every quote for one client.

    Module 1.4 says an SCC "overrides the matching standard clause for
    that quote **or client**". A client we have agreed 60-day payment
    with has agreed it for all their work, and re-typing it on each
    quote is how one quote goes out at 30.

    Its own table rather than a nullable `quote_id` on
    `quote_special_conditions`, because that column is a NOT NULL
    foreign key on a table that already holds rows, and widening a
    constraint under live data to save a table is a poor trade. The
    two are read together by `quote_terms.conditions_for()`.
    """

    __tablename__ = 'account_standing_conditions'

    id          = db.Column(db.Integer, primary_key=True)
    account_id  = db.Column(db.Integer,
                            db.ForeignKey('companies.id'),
                            nullable=False, index=True)
    seq         = db.Column(db.Integer, nullable=False, default=1)
    text        = db.Column(db.Text, nullable=False)
    #: Why it stands — the mail or the contract it came from.
    source      = db.Column(db.String(300))
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    created_by  = db.Column(db.String(20))
    #: Ended rather than deleted: a quote printed under it can still
    #: say so.
    ended_at    = db.Column(db.DateTime, nullable=True)
    ended_by    = db.Column(db.String(20))

    __table_args__ = (
        db.Index('ix_standing_account_live', 'account_id', 'ended_at'),
    )

    def to_dict(self):
        return {'id': self.id, 'account_id': self.account_id,
                'seq': self.seq, 'text': self.text or '',
                'source': self.source or '',
                'live': self.ended_at is None}
