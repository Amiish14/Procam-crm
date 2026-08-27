"""
v2026-08 — Process a single email → Lead. Reused by both the poll pipeline
and the webhook handler so both routes end at the same DB state.

Public function:

    process_single_message(graph, mailbox, msg) -> dict

The message dict is a Microsoft Graph "message" resource (as returned by
list_messages / get_message). Returns a status dict:

    {'status': 'created' | 'skipped' | 'failed',
     'reason': str,                            # only when skipped / failed
     'lead_id': int | None,
     'internet_message_id': str | None}
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import Optional

from . import parser as email_parser
from . import ai_extractor
from . import attachments as attachments_mod

log = logging.getLogger(__name__)

DEDUP_DOMAIN_WINDOW_DAYS = 30


def _internet_message_id(msg: dict) -> Optional[str]:
    return msg.get('internetMessageId') or msg.get('id')


def process_single_message(graph, mailbox: str, msg: dict) -> dict:
    """Process ONE Graph message into a Lead. Idempotent — safe to call
    multiple times for the same message; only the first call creates a
    Lead. Any subsequent call returns 'skipped' with reason='already ingested'.
    """
    # Local imports keep this module import-safe under all circumstances.
    from app import app, db, Lead, LeadAttachment  # type: ignore

    imid = _internet_message_id(msg)

    with app.app_context():
        # ── Idempotency ────────────────────────────────────────────────
        if imid:
            hit = (db.session.query(Lead.id)
                   .filter(Lead.email_message_id == imid).first())
            if hit:
                return {'status': 'skipped', 'reason': 'already ingested',
                        'lead_id': hit[0], 'internet_message_id': imid}

        # ── Parse + AI-extract ────────────────────────────────────────
        try:
            extracted = email_parser.extract_lead(msg)
        except Exception as e:
            log.exception('parser exception for %s: %s', imid, e)
            return {'status': 'failed', 'reason': f'parser exception: {e}',
                    'lead_id': None, 'internet_message_id': imid}
        if extracted is None:
            return {'status': 'skipped', 'reason': 'parser returned None',
                    'lead_id': None, 'internet_message_id': imid}
        if extracted.get('skip_reason'):
            return {'status': 'skipped',
                    'reason': extracted['skip_reason'],
                    'lead_id': None, 'internet_message_id': imid}

        # AI merge (regex fills gaps AI missed; AI wins where both agree)
        try:
            ai = ai_extractor.extract(msg, extracted)
            if ai:
                for k, v in ai.items():
                    if v and not extracted.get(k):
                        extracted[k] = v
        except Exception:
            log.exception('AI extractor failed silently — continuing on regex')

        # ── DB-level dedup (email + recent domain) ─────────────────────
        # v2026-08 — when a Procam employee EXPLICITLY forwards a lead to
        # the CRM inbox, honour their intent: always create the lead even
        # if that customer already exists in the system. Same customer can
        # legitimately raise multiple RFQs; the sales team can merge later
        # in the UI if it's actually the same enquiry.
        forwarded_by = extracted.get('forwarded_by') or msg.get('_forwarded_by')
        sender_email  = (extracted.get('email') or '').strip().lower()
        sender_domain = sender_email.split('@', 1)[1] if '@' in sender_email else ''

        if not forwarded_by:
            if sender_email:
                if db.session.query(Lead.id).filter(
                        db.func.lower(Lead.email) == sender_email).first():
                    return {'status': 'skipped', 'reason': 'existing lead: same email',
                            'lead_id': None, 'internet_message_id': imid}

            if sender_domain:
                cutoff = datetime.utcnow() - timedelta(days=DEDUP_DOMAIN_WINDOW_DAYS)
                recent_hit = db.session.query(Lead.id).filter(
                    Lead.email.isnot(None),
                    Lead.email.op('ILIKE')(f'%@{sender_domain}'),
                    Lead.created_at >= cutoff,
                ).first()
                if recent_hit:
                    return {'status': 'skipped',
                            'reason': f'existing lead: same domain <{DEDUP_DOMAIN_WINDOW_DAYS}d',
                            'lead_id': None, 'internet_message_id': imid}

        # ── Create Lead — reuses model fields shared with existing routes.
        message_body = extracted.get('body') or extracted.get('body_text') or None
        if forwarded_by and message_body:
            who = forwarded_by.get('email') if isinstance(forwarded_by, dict) else str(forwarded_by)
            message_body = (
                f"[Forwarded to CRM by {who} on "
                f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}]\n\n"
                + message_body
            )

        try:
            lead = Lead(
                source              = 'email',
                stage               = 'New',
                name                = extracted.get('name')    or (sender_email or 'Unknown'),
                email               = extracted.get('email')   or None,
                mobile              = extracted.get('phone')   or None,
                company             = extracted.get('company') or None,
                subject             = extracted.get('subject') or msg.get('subject'),
                message             = message_body,
                email_message_id    = imid,
                ai_summary_json     = extracted.get('ai_summary_json'),
                created_at          = datetime.utcnow(),
            )
            db.session.add(lead)
            db.session.flush()

            # ── Attachments ─────────────────────────────────────────────
            try:
                attachments_mod.save_attachments_for_lead(
                    graph, mailbox=mailbox,
                    message_id=msg.get('id'), lead_id=lead.id,
                )
            except Exception:
                log.exception('attachments save failed for lead %s', lead.id)

            db.session.commit()
            return {'status': 'created', 'lead_id': lead.id,
                    'internet_message_id': imid, 'reason': None}
        except Exception as e:
            db.session.rollback()
            log.exception('lead insert failed for %s', imid)
            return {'status': 'failed', 'reason': f'lead insert: {e}',
                    'lead_id': None, 'internet_message_id': imid}
