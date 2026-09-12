"""
Seed the email trail from an ingested message.

Called by both ingest paths (pipeline.py for the batch poll,
single_message.py for the webhook) so a lead created either way opens
with its enquiry already in the thread rather than only in the
original_email_* columns.

Best-effort: a failure here must never lose the lead itself.
"""
import logging

log = logging.getLogger(__name__)


def record_inbound(db, lead, *, subject=None, from_addr=None, to_addr=None,
                   body=None, received_at=None, message_id=None):
    """Add the inbound enquiry as row 1 of the lead's email trail.

    Idempotent on message_id, so a replayed or redelivered message does
    not duplicate the thread.
    """
    try:
        from app import LeadEmail
    except Exception:
        return None

    body = body or getattr(lead, 'original_email_body', None)
    if not body:
        return None

    try:
        if message_id:
            existing = LeadEmail.query.filter_by(
                lead_id=lead.id, message_id=message_id).first()
            if existing:
                return existing

        row = LeadEmail(
            lead_id=lead.id,
            direction='inbound',
            from_addr=(from_addr
                       or getattr(lead, 'original_email_from', None)),
            to_addr=to_addr,
            subject=(subject
                     or getattr(lead, 'original_email_subject', None)),
            body=body,
            sent_or_received_at=(received_at
                                 or getattr(lead, 'original_email_received_at',
                                            None)
                                 or lead.created_at),
            source='ingested',
            status='received',
            message_id=message_id or getattr(lead, 'email_message_id', None),
        )
        db.session.add(row)
        return row
    except Exception:
        log.exception('could not seed the email trail for lead %s',
                      getattr(lead, 'id', '?'))
        return None
