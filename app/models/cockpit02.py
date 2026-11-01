"""
CRM-COCKPIT-02 — the two tables this release needs.

`lead_quote_due_history` is every move of a quote submission due date
(§2.4). A due date that changes is the normal case, not the exception:
a client extends, a corrigendum lands, we ask for more time. What a
reviewer needs afterwards is not the current date but the sequence —
"this closed on the 29th, was extended twice, and we still did not
bid" is a conversation; "this closed on the 4th" is not.

`crm_bulk_change_log` is the reversible log §8 asks for. Every bulk
change in this release writes one row per field it touches, with the
old value and the new one, under a batch name. `scripts/undo_batch.py`
reads it back. The rule it exists to serve: a bulk change nobody can
undo is a bulk change nobody should run.

Why a log table and not the audit trail: `audit_events` already
records every change and is append-only, which is right for "what
happened" and wrong for "put it back". An undo needs the old value in
a typed column it can write back, the rows of one batch together, and
a mark saying the undo has already run so it cannot run twice.
"""
from datetime import datetime

from app import db

#: §2.4 — why a due date moved. A code, not free text, because
#: "how often do clients extend, and how often do we ask" is a
#: question somebody will want counted.
EXTENSION_REASONS = (
    ('client_extension', 'Client extension'),
    ('corrigendum', 'Corrigendum'),
    ('our_request', 'Extension on our request'),
    ('other', 'Other'),
)
EXTENSION_CODES = tuple(code for code, _label in EXTENSION_REASONS)
EXTENSION_LABELS = dict(EXTENSION_REASONS)


class LeadQuoteDueHistory(db.Model):
    """One row per move of a lead's quote submission due date."""
    __tablename__ = 'lead_quote_due_history'

    id = db.Column(db.Integer, primary_key=True)
    lead_id = db.Column(db.Integer, index=True, nullable=False)

    #: Null on the first row — the date arrived where there was none.
    old_due_at = db.Column(db.DateTime)
    new_due_at = db.Column(db.DateTime)

    #: One of EXTENSION_CODES. 'set' for the first date on a lead,
    #: which is not an extension and must not be counted as one.
    reason_code = db.Column(db.String(24), nullable=False, default='set')
    reason_text = db.Column(db.String(300))

    #: The mail that moved it, where there is one — the alert, the
    #: corrigendum, the client's reply. Kept so the history can link
    #: back to the evidence rather than asserting it.
    source = db.Column(db.String(20))
    email_message_id = db.Column(db.String(255))

    #: Who accepted it. 'system' for a date read from a mail that
    #: nobody has confirmed yet.
    actor = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        def dt(value):
            return value.isoformat(timespec='minutes') if value else ''

        return {
            'id': self.id,
            'lead_id': self.lead_id,
            'old_due_at': dt(self.old_due_at),
            'new_due_at': dt(self.new_due_at),
            'reason_code': self.reason_code or '',
            'reason': EXTENSION_LABELS.get(self.reason_code,
                                           self.reason_code or ''),
            'reason_text': self.reason_text or '',
            'source': self.source or '',
            'actor': self.actor or '',
            'at': dt(self.created_at),
        }


class BulkChangeLog(db.Model):
    """One row per field a bulk change touched, so it can be undone.

    Values are stored as text on purpose. A column that has to hold a
    date, a number, a name and a null is a text column whichever way
    it is declared, and the undo writes through the ORM attribute, so
    the model's own type coercion puts it back correctly.
    """
    __tablename__ = 'crm_bulk_change_log'

    id = db.Column(db.Integer, primary_key=True)
    #: The run this row belongs to, e.g.
    #: 'cockpit02.followup_backfill.2026-11-01'. One undo command
    #: takes a batch.
    batch = db.Column(db.String(80), nullable=False, index=True)
    #: What the batch was for, in words, so the log reads without the
    #: prompt beside it.
    note = db.Column(db.String(300))

    entity_type = db.Column(db.String(40), nullable=False)
    entity_id = db.Column(db.String(40), nullable=False, index=True)
    field = db.Column(db.String(60), nullable=False)

    old_value = db.Column(db.Text)
    new_value = db.Column(db.Text)
    #: True when the field held nothing before. Distinguishes "it was
    #: empty" from "it was the string 'None'", which is the bug every
    #: hand-rolled undo has.
    was_null = db.Column(db.Boolean, default=False)

    actor = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    #: Stamped by the undo. A row that has been put back is not
    #: eligible again, so running the undo twice is safe.
    undone_at = db.Column(db.DateTime)
    undone_by = db.Column(db.String(20))

    def to_dict(self):
        return {
            'id': self.id,
            'batch': self.batch,
            'entity': f'{self.entity_type}#{self.entity_id}',
            'field': self.field,
            'old': '' if self.was_null else (self.old_value or ''),
            'new': self.new_value or '',
            'actor': self.actor or '',
            'at': (self.created_at.isoformat(timespec='seconds')
                   if self.created_at else ''),
            'undone': bool(self.undone_at),
        }
