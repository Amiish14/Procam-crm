"""
The tables behind outbound email: what was captured, what is queued,
who wants what, and what the week looked like when it was frozen.

Five tables, all additive, none of them touched by anything that
existed before:

    lead_raw_emails          the original RFQ, kept as a .eml file so it
                             can be forwarded exactly as the client
                             sent it
    email_outbox             every outbound message, written in the same
                             transaction as the thing that caused it and
                             sent by a separate worker
    notification_prefs       one row per person — channels, batching,
                             quiet hours
    weekly_pipeline_snapshot the numbers as they stood at the Friday
                             freeze, so a review cannot be argued with
                             by refreshing the page
    job_leases               which process is currently allowed to run a
                             given job. Two gunicorn workers and a
                             systemd timer must not send the same email
                             twice.
"""
from datetime import datetime

from app import db


class LeadRawEmail(db.Model):
    """The client's original message, byte for byte.

    The CRM has always kept a *rendering* of an inbound email — subject,
    sender, a truncated body — which is enough to read and not enough to
    forward. The person who has to quote has to go and ask a colleague
    to forward the real thing. This row is the file that ends that.
    """
    __tablename__ = 'lead_raw_emails'

    id = db.Column(db.Integer, primary_key=True)
    lead_id = db.Column(db.Integer, index=True, nullable=False)
    #: The row in the lead's email trail this is the original of, when
    #: it is known. Null for a back-fill that matched on message id only.
    lead_email_id = db.Column(db.Integer, index=True)

    #: RFC-5322 Message-Id. Unique: one stored original per message, and
    #: the database rather than a query is what guarantees it when the
    #: webhook and the five-minute poll see the same mail at once.
    internet_message_id = db.Column(db.String(400), unique=True, index=True)
    #: Graph's own id. Kept because it is what the attachment and MIME
    #: endpoints take, so a re-fetch does not need a second lookup.
    graph_message_id = db.Column(db.String(400))

    subject = db.Column(db.String(500))
    from_addr = db.Column(db.String(320))
    received_at = db.Column(db.DateTime)

    storage_path = db.Column(db.String(600))
    size_bytes = db.Column(db.Integer)
    #: Of the .eml bytes. Proves the file on disk is the one captured,
    #: and lets a re-capture notice it already has this message.
    sha256 = db.Column(db.String(64), index=True)

    #: stored | missing | failed. `missing` means the mailbox no longer
    #: has it — moved, deleted or past retention — which is a fact worth
    #: recording rather than retrying forever.
    status = db.Column(db.String(16), default='stored', index=True)
    error = db.Column(db.String(400))
    captured_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'lead_id': self.lead_id,
            'subject': self.subject or '',
            'from_addr': self.from_addr or '',
            'received_at': str(self.received_at)[:16] if self.received_at else '',
            'size_bytes': self.size_bytes or 0,
            'status': self.status or 'stored',
        }


class EmailOutbox(db.Model):
    """One outbound email, queued rather than sent.

    Why a queue and not a `send()` where the work happens: Graph is a
    network call that can take seconds and can fail. Doing it inside the
    request means a slow mail server makes the CRM slow, and doing it
    inside the transaction means a failed send can roll back the
    assignment that caused it. Writing a row is neither. The worker then
    owns delivery, retries and the record of what happened.
    """
    __tablename__ = 'email_outbox'

    id = db.Column(db.Integer, primary_key=True)

    #: What this message is about, in a form the sender can be asked to
    #: produce only once. Unique — the idempotency guarantee. Two
    #: workers, a replayed webhook or a double-clicked button all write
    #: the same key and the second write is refused by the database.
    dedupe_key = db.Column(db.String(200), unique=True, index=True)

    #: The matrix event (notification_rules.MATRIX) or a report name.
    event_key = db.Column(db.String(60), index=True)
    #: The colleague this is for, when there is one. Reports to a
    #: distribution list leave it null.
    user_code = db.Column(db.String(20), index=True)

    to_addr = db.Column(db.String(1000), nullable=False)
    cc_addr = db.Column(db.String(1000))
    subject = db.Column(db.String(400), nullable=False)
    html = db.Column(db.Text, nullable=False)
    text_body = db.Column(db.Text)

    #: [{"kind": "lead_attachment", "id": 12}, {"kind": "raw_email",
    #:   "id": 3}] — references, not bytes. The file is read at send
    #: time, so a 9 MB queue row never exists and a file deleted in the
    #: meantime is simply not attached.
    attachments_json = db.Column(db.Text)

    entity_type = db.Column(db.String(40))
    entity_id = db.Column(db.String(60))

    #: queued | sending | sent | failed | blocked | cancelled
    status = db.Column(db.String(16), default='queued', index=True)
    attempts = db.Column(db.Integer, default=0)
    max_attempts = db.Column(db.Integer, default=5)

    #: Not before this moment. Carries three different ideas with one
    #: column: the retry back-off, the end of quiet hours, and the end
    #: of a batching window.
    not_before = db.Column(db.DateTime, index=True, default=datetime.utcnow)
    #: Messages sharing a batch key and still queued are rolled into one
    #: email by the worker, so fifteen leads assigned at once arrive as
    #: one message rather than fifteen.
    batch_key = db.Column(db.String(120), index=True)

    claimed_by = db.Column(db.String(80))
    claimed_at = db.Column(db.DateTime)
    sent_at = db.Column(db.DateTime)
    last_error = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        return {
            'id': self.id, 'event_key': self.event_key,
            'to': self.to_addr, 'subject': self.subject,
            'status': self.status, 'attempts': self.attempts or 0,
            'not_before': str(self.not_before)[:16] if self.not_before else '',
            'sent_at': str(self.sent_at)[:16] if self.sent_at else '',
            'error': self.last_error or '',
            'created_at': str(self.created_at)[:16] if self.created_at else '',
        }


class NotificationPref(db.Model):
    """What one person wants to be told, and when.

    The defaults are the behaviour that existed before this table, so a
    person with no row is unaffected by its arrival.
    """
    __tablename__ = 'notification_prefs'

    id = db.Column(db.Integer, primary_key=True)
    user_code = db.Column(db.String(20), unique=True, index=True,
                          nullable=False)

    email_enabled = db.Column(db.Boolean, default=True)
    #: immediate | batched — batched holds an email back for
    #: `batch_minutes` so several in a row arrive together.
    mode = db.Column(db.String(16), default='immediate')
    batch_minutes = db.Column(db.Integer, default=15)

    #: Evening and morning boundaries in IST, as whole hours. An email
    #: raised inside the window waits until the morning — except the
    #: ones marked urgent, which never wait.
    quiet_enabled = db.Column(db.Boolean, default=True)
    quiet_start_hour = db.Column(db.Integer, default=21)
    quiet_end_hour = db.Column(db.Integer, default=7)

    #: {"lead.assigned": false} — only the events the person has turned
    #: off are stored, so a new event in the matrix defaults to on
    #: rather than silently off.
    muted_events_json = db.Column(db.Text)

    daily_brief = db.Column(db.Boolean, default=True)
    weekly_pack = db.Column(db.Boolean, default=True)

    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)


class WeeklyPipelineSnapshot(db.Model):
    """The pipeline as it stood when the week was frozen.

    A review that opens the live screen argues with a number that moved
    while people were talking. The Friday freeze writes the numbers
    down; the meeting reads the written ones.
    """
    __tablename__ = 'weekly_pipeline_snapshot'

    id = db.Column(db.Integer, primary_key=True)
    #: ISO week, e.g. '2026-W40'. With scope_key it is unique, so a
    #: freeze run twice in a week replaces nothing and adds nothing.
    week_label = db.Column(db.String(12), index=True, nullable=False)
    #: 'company', 'vertical:Projects', 'person:EMP123'.
    scope_key = db.Column(db.String(80), index=True, nullable=False)
    scope_label = db.Column(db.String(160))

    taken_at = db.Column(db.DateTime, default=datetime.utcnow)

    open_count = db.Column(db.Integer, default=0)
    open_value = db.Column(db.Numeric(18, 2))
    won_count = db.Column(db.Integer, default=0)
    won_value = db.Column(db.Numeric(18, 2))
    lost_count = db.Column(db.Integer, default=0)
    quotes_pending = db.Column(db.Integer, default=0)
    overdue_count = db.Column(db.Integer, default=0)
    #: Everything else the pack showed, so a later question about the
    #: meeting can be answered without recomputing history.
    payload_json = db.Column(db.Text)

    __table_args__ = (
        db.UniqueConstraint('week_label', 'scope_key',
                            name='uq_weekly_snapshot_week_scope'),
    )


class JobLease(db.Model):
    """Which process may run a job right now.

    Two gunicorn workers and a systemd timer share one SQLite file.
    Postgres would answer this with an advisory lock; here the answer is
    a row with an expiry, taken with a conditional UPDATE that only one
    writer can win because SQLite serialises writes. A holder that dies
    loses the lease when it expires rather than holding it forever.
    """
    __tablename__ = 'job_leases'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60), unique=True, index=True, nullable=False)
    holder = db.Column(db.String(120))
    acquired_at = db.Column(db.DateTime)
    expires_at = db.Column(db.DateTime, index=True)
    note = db.Column(db.String(200))
