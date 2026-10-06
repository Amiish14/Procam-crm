"""
What happened to every message that reached the leads mailbox.

The ingest already decides a great deal — this is a lead, this is a
reply, this is a newsletter, this is a duplicate — and until now most
of those decisions left no durable trace that an administrator could
read. A message that was skipped for a good reason and a message that
was skipped by mistake looked identical: both were silence.

One row per message, whatever the outcome, including the ones that
were refused. That makes two questions answerable that could not be
answered before: "did our enquiry reach the CRM?" and "why is it not
a lead?"

`created` | `attached` | `reopened` | `skipped` | `error` — and the
screen at /CRM/admin/mail-ingest can turn a `skipped` row into a lead
when the classifier got it wrong, which is the point of keeping them.
"""
from datetime import datetime

from app import db

CREATED = 'created'
ATTACHED = 'attached'
REOPENED = 'reopened'
SKIPPED = 'skipped'
ERROR = 'error'
OUTCOMES = (CREATED, ATTACHED, REOPENED, SKIPPED, ERROR)


class MailIngestLog(db.Model):
    __tablename__ = 'mail_ingest_log'

    id = db.Column(db.Integer, primary_key=True)

    #: When the message reached the mailbox, not when we processed it.
    received_at = db.Column(db.DateTime, index=True)

    #: RFC-5322 Message-Id. Indexed and unique: this is the one key the
    #: ingest deduplicates on, and the database rather than a query is
    #: what guarantees it when the webhook and the five-minute poll see
    #: the same mail at the same moment.
    internet_message_id = db.Column(db.String(400), unique=True, index=True)
    conversation_id = db.Column(db.String(400), index=True)

    from_addr = db.Column(db.String(320), index=True)
    subject = db.Column(db.String(500))

    outcome = db.Column(db.String(16), index=True, nullable=False)
    lead_id = db.Column(db.Integer, index=True)
    #: In a sentence, for a person. "Sender on the newsletter list",
    #: "already ingested", "no client could be identified".
    reason = db.Column(db.String(500))

    classifier_label = db.Column(db.String(40))
    confidence = db.Column(db.Integer)

    raw_eml_path = db.Column(db.String(600))

    #: Set when an administrator turns a skipped message into a lead
    #: from the screen, so the override is visible beside the original
    #: decision rather than replacing it.
    overridden_by = db.Column(db.String(20))
    overridden_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'received_at': str(self.received_at)[:16] if self.received_at else '',
            'internet_message_id': self.internet_message_id or '',
            'conversation_id': self.conversation_id or '',
            'from_addr': self.from_addr or '',
            'subject': self.subject or '',
            'outcome': self.outcome,
            'lead_id': self.lead_id,
            'reason': self.reason or '',
            'classifier_label': self.classifier_label or '',
            'confidence': self.confidence,
            'overridden_by': self.overridden_by or '',
        }
