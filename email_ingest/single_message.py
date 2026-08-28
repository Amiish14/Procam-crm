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
from . import ai_router                                              # noqa
from . import attachments as attachments_mod
from . import enrich as enrich_mod

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
        # ── Idempotency (kept — same physical email must not double-insert) ──
        if imid:
            hit = (db.session.query(Lead.id)
                   .filter(Lead.email_message_id == imid).first())
            if hit:
                return {'status': 'skipped', 'reason': 'already ingested',
                        'lead_id': hit[0], 'internet_message_id': imid}

        # ── Parse + AI-extract ────────────────────────────────────────
        # v2026-08 — mailbox mode: CAPTURE EVERYTHING. If the parser wants
        # to skip (auto-reply, bulk sender, low confidence, whatever), we
        # still create the lead. The skip_reason is preserved as a
        # 'triage_tag' inside the lead's opp_notes so the sales team can
        # sort them in the UI. Silently dropping mail = losing business.
        try:
            extracted = email_parser.extract_lead(msg)
        except Exception as e:
            log.exception('parser exception for %s: %s', imid, e)
            extracted = None

        if not extracted:
            # Even if the parser completely bailed, build a minimal payload
            # from the raw Graph message so we still get a lead row.
            from_addr = ((msg.get('from') or {}).get('emailAddress') or {})
            extracted = {
                'company': '',
                'contact_name': from_addr.get('name') or '',
                'email': from_addr.get('address') or '',
                'phone': None,
                'subject': msg.get('subject') or '',
                'body_text': msg.get('bodyPreview') or '',
                'signals': {},
                'confidence': 0.0,
                'skip_reason': 'parser bailed',
            }

        triage_tag = extracted.get('skip_reason')       # None if clean

        forwarded_by = extracted.get('forwarded_by') or msg.get('_forwarded_by')
        sender_email  = (extracted.get('email') or '').strip().lower()
        sender_domain = sender_email.split('@', 1)[1] if '@' in sender_email else ''
        # DB dedup by email / recent domain intentionally REMOVED. Users
        # explicitly asked for zero-skip behaviour; same customer emailing
        # a second time creates a second lead row, and the sales team can
        # merge or close in the UI.

        # ── Build Lead payload — shared enricher, same summary card poll uses.
        try:
            lead_kwargs = enrich_mod.build_enriched_lead_kwargs(
                msg, extracted,
                sender_email=sender_email,
                sender_domain=sender_domain,
                forwarded_by=forwarded_by,
            )
        except Exception as e:
            log.exception('enricher failed for %s', imid)
            return {'status': 'failed', 'reason': f'enricher: {e}',
                    'lead_id': None, 'internet_message_id': imid}

        # If the parser flagged a triage reason, surface it in opp_notes so
        # the sales team can filter (e.g. hide 'auto-reply' rows).
        if triage_tag:
            try:
                import json as _json
                cur = _json.loads(lead_kwargs.get('opp_notes') or '{}')
                cur['triage_tag'] = triage_tag
                lead_kwargs['opp_notes'] = _json.dumps(cur, default=str)
            except Exception:
                pass

        try:
            lead = Lead(
                source            = 'email',
                stage             = 'New Opportunity',
                email_message_id  = imid,
                created_at        = datetime.utcnow(),
                **lead_kwargs,
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
