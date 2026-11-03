"""Every notification the CRM tried to send, and what became of it.

CRM-USER-ISSUES-02NOV §3: "Some PICs don't get the new-lead mail. Find
how the recipient is resolved and where it fails … Log every
notification (lead, recipient, address used, sent/failed, error)."

WHY THE OUTBOX IS NOT ALREADY THIS

`email_outbox` is excellent and it is not the answer. It records every
message that *reached* it, with attempts, back-off, an SMTP code and
an admin screen at /admin/email/log. The notifications §3 is about
never reached it. `notify.send` has five ways to decide there is
nothing to queue:

    * the employee has no `email` on file;
    * `outbox.enqueue` returns None — already queued, muted, or not an
      internal address, which `mail_policy` refuses;
    * FEATURE_EMAIL_NOTIFY is off, so the direct transport is used and
      nothing is written down at all;
    * the notification is suppressed as a recent duplicate, or
      collected by a bulk run;
    * anything raises, and the exception is logged and swallowed.

Each of those returned a dict with an `error` in it, and the one
caller that matters — `lead_assignment` — threw the dict away. So a
PIC who never got their mail and a PIC who did looked identical from
inside the CRM: both were silence, in exactly the way
`mail_ingest_log` was written to stop happening for inbound mail.

This is that table for outbound notifications. One row per attempt,
whatever the outcome, including the refusals.

WHERE IT IS WRITTEN

In `notify.send`, which is the one function every notification goes
through. Not in `lead_assignment`: writing it at the call site would
log the assignment mail and miss the other forty callers, which is the
same mistake as filtering archived leads in thirty-three modules and
not in the scope.

THE ADDRESS IS STORED EVEN WHEN IT WAS REFUSED

`address` is what the CRM actually tried, not what it should have
been. A row saying `status='failed'`, `address='somebody@gmail.com'`,
`error='not an internal address'` answers the question in one line.
Storing the intended address only when it worked would hide the single
most common cause.
"""
from datetime import datetime

from app import db

#: Queued into the outbox. The outbox owns it from here, and
#: `outbox_id` says which row to look at.
QUEUED = 'queued'
#: Handed to the transport directly and accepted. Only the
#: FEATURE_EMAIL_NOTIFY-off path produces this.
SENT = 'sent'
#: Tried and did not go. `error` says why.
FAILED = 'failed'
#: Deliberately not sent — a recent duplicate, a bulk run collecting
#: them, or notifications switched off. Not a fault, and kept apart
#: from `failed` so the failure screen is not buried in them.
SUPPRESSED = 'suppressed'
#: The bell row was written and no email was asked for. The common
#: case, and recorded so "was anybody told at all?" is answerable.
IN_APP_ONLY = 'in_app'

STATUSES = (QUEUED, SENT, FAILED, SUPPRESSED, IN_APP_ONLY)

#: The statuses a person needs to see on the failure screen.
NEEDS_ATTENTION = (FAILED,)


class NotificationAttempt(db.Model):
    """One attempt to tell one person one thing."""

    __tablename__ = 'notification_attempts'

    id = db.Column(db.Integer, primary_key=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    #: The colleague this was for, as an employee code. Never a name:
    #: people are data.
    user_code = db.Column(db.String(20), index=True)

    #: The address actually used, or null where resolution is what
    #: failed. See the module docstring.
    address = db.Column(db.String(320))

    #: `kind` is the bell category; `event_key` is the matrix event
    #: that chose the recipients. Both, because a reader asking "why
    #: did this go out?" needs the event and "what was it?" needs the
    #: kind.
    kind = db.Column(db.String(40), index=True)
    event_key = db.Column(db.String(60), index=True)

    #: What it was about. For §3 this is ('Lead', <lead id>), which is
    #: what makes "which leads did nobody hear about?" a query.
    entity_type = db.Column(db.String(40), index=True)
    entity_id = db.Column(db.String(40), index=True)

    title = db.Column(db.String(200))

    status = db.Column(db.String(16), index=True, nullable=False)
    #: In a sentence, as `notify.send` reported it.
    error = db.Column(db.String(300))

    #: The outbox row that owns delivery, where one was created. The
    #: two tables together are the whole story: this one says whether
    #: the CRM got as far as queueing, that one says what the mail
    #: server then did.
    outbox_id = db.Column(db.Integer, index=True)

    #: How many times the direct transport was tried. The outbox has
    #: its own `attempts` and owns retry for anything queued; this
    #: counts only the FEATURE_EMAIL_NOTIFY-off path, which had no
    #: retry at all before §3.
    attempts = db.Column(db.Integer, default=0)

    #: Set when somebody re-sent it from the failure screen, so the
    #: retry is visible beside the original rather than replacing it.
    retried_by = db.Column(db.String(20))
    retried_at = db.Column(db.DateTime)

    def to_dict(self):
        return {
            'id': self.id,
            'at': str(self.created_at or '')[:16],
            'user_code': self.user_code or '',
            'address': self.address or '',
            'kind': self.kind or '',
            'event_key': self.event_key or '',
            'entity_type': self.entity_type or '',
            'entity_id': self.entity_id or '',
            'title': self.title or '',
            'status': self.status,
            'error': self.error or '',
            'outbox_id': self.outbox_id,
            'attempts': self.attempts or 0,
            'retried_by': self.retried_by or '',
            'retried_at': str(self.retried_at or '')[:16],
        }
