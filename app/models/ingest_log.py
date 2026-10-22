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
#: Mail from a machine — a quarantine digest, a bounce, a
#: postmaster notice. Its own outcome rather than `skipped`,
#: because skipped is where "already ingested" and "already
#: classified" live and an administrator looking for what the
#: sender rules held should not have to read past 488 of those.
BLOCKED = 'blocked'
OUTCOMES = (CREATED, ATTACHED, REOPENED, SKIPPED, BLOCKED, ERROR)


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

    #: Who forwarded it in — the internal From of the outermost
    #: message, and their employee code where the address matches
    #: somebody on file. This is what makes My Mail possible: without
    #: it the CRM knows a mail arrived and not whose question it
    #: answers. Null where the sender was external or unrecognised; a
    #: guess here would put one person's mail on another's page.
    forwarded_by = db.Column(db.String(200))
    forwarded_by_emp = db.Column(db.String(20), index=True)

    #: What the lead's stage was at the moment the mail landed on it.
    #: "It was attached to a Lost lead" is the whole explanation for
    #: the Pandacs case, and the stage has usually moved by the time
    #: anybody asks.
    lead_stage_at_ingest = db.Column(db.String(40))

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
            'forwarded_by': self.forwarded_by or '',
            'forwarded_by_emp': self.forwarded_by_emp or '',
            'lead_stage_at_ingest': self.lead_stage_at_ingest or '',
            'has_original': bool(self.raw_eml_path),
        }
