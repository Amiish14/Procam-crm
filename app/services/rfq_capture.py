"""
Capture the request as the client sent it.

The complaint this exists to answer: a lead arrives, the CRM shows a
summary of the email, and the person who has to quote it still has to
ask a colleague to forward the original with the drawings attached.
Everything needed was in the mailbox; none of it reached the person.

Two things are captured, and they are different things:

  * the **attachments** — the drawings, the BOQ, the packing list. The
    files were already being written to disk, but on the webhook path
    (which is the production path) the database row that makes them
    visible was never written, so they sat there unreachable. See
    `record_attachments`.
  * the **original message** — the `.eml`, so it can be forwarded
    exactly as received, headers and all. See `email_ingest.raw_mime`.

One function does both, because every caller wants both, and doing it
in one place is how the webhook and the poll stop disagreeing.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def record_attachments(lead_id, saved):
    """Write the rows for files already on disk. Returns the rows added.

    Idempotent two ways: by Graph's attachment id where there is one,
    and by the storage path where there is not. Running this after a
    re-ingest of the same message adds nothing.
    """
    from app import LeadAttachment, db

    added = []
    for meta in (saved or []):
        try:
            att_id = meta.get('email_attachment_id') or None
            path = meta.get('storage_path')
            query = LeadAttachment.query.filter_by(lead_id=lead_id)
            existing = (query.filter_by(email_attachment_id=att_id).first()
                        if att_id else
                        query.filter_by(storage_path=path).first())
            if existing is not None:
                continue
            row = LeadAttachment(
                lead_id=lead_id,
                filename=meta['filename'],
                content_type=meta.get('content_type'),
                size_bytes=meta.get('size_bytes'),
                storage_path=path,
                source='email',
                email_attachment_id=att_id,
            )
            db.session.add(row)
            added.append(row)
        except Exception:
            log.exception('could not record an attachment row for lead %s',
                          lead_id)
    return added


def capture_for_lead(lead, *, graph, mailbox, msg, lead_email_id=None,
                     commit=False):
    """Everything worth keeping from one inbound message.

    Returns {'attachments': n, 'raw': 'stored'|'missing'|'failed'|'off',
             'raw_id': id|None}. Never raises: the lead already exists
    and nothing here is worth losing it over.
    """
    from email_ingest import attachments as attachments_mod
    from app.services import flags

    out = {'attachments': 0, 'raw': 'off', 'raw_id': None}
    lead_id = getattr(lead, 'id', None)
    if not lead_id:
        return out

    graph_id = (msg or {}).get('id')
    imid = (msg or {}).get('internetMessageId')

    # ── the documents ────────────────────────────────────────────────
    try:
        if (msg or {}).get('hasAttachments') and graph_id:
            saved = attachments_mod.save_attachments_for_lead(
                graph, mailbox=mailbox, message_id=graph_id, lead_id=lead_id)
            rows = record_attachments(lead_id, saved)
            out['attachments'] = len(rows)
    except Exception:
        log.exception('attachment capture failed for lead %s', lead_id)

    # ── the envelope ─────────────────────────────────────────────────
    if not flags.on('FEATURE_RFQ_CAPTURE'):
        return out
    try:
        from email_ingest import raw_mime
        sender = (((msg or {}).get('from') or {}).get('emailAddress')
                  or {}).get('address') or ''
        row = raw_mime.capture(
            lead_id, graph=graph, mailbox=mailbox,
            graph_message_id=graph_id, internet_message_id=imid,
            subject=(msg or {}).get('subject') or '',
            from_addr=sender,
            received_at=getattr(lead, 'created_at', None),
            lead_email_id=lead_email_id, commit=commit)
        if row is not None:
            out['raw'] = row.status or 'failed'
            out['raw_id'] = row.id
    except Exception:
        log.exception('original-email capture failed for lead %s', lead_id)
        out['raw'] = 'failed'
    return out


def summary_for_lead(lead_id):
    """What the lead drawer shows: the files, and the original.

    Used by the API the drawer calls and by the email templates, so the
    chip in the browser and the line in the email cannot disagree.
    """
    from app import LeadAttachment

    try:
        files = (LeadAttachment.query.filter_by(lead_id=lead_id)
                 .order_by(LeadAttachment.uploaded_at.desc()).all())
    except Exception:
        files = []
    try:
        from email_ingest import raw_mime
        originals = raw_mime.for_lead(lead_id)
    except Exception:
        originals = []
    stored = [o for o in originals if (o.status == 'stored'
                                       and o.storage_path
                                       and os.path.exists(o.storage_path))]
    return {
        'attachments': [a.to_dict() for a in files],
        'attachment_count': len(files),
        'original': stored[0].to_dict() if stored else None,
        'original_id': stored[0].id if stored else None,
        'originals': [o.to_dict() for o in originals],
    }
