"""
v2026-08 — Process a single email → Lead. Reused by both the poll pipeline
and the webhook handler so both routes end at the same DB state.

v2026-09 — CAPTURE EVERYTHING. leads@procamgroup.in exists for one purpose:
employees forward leads into it. So the mailbox itself is the filter, and
every message that arrives becomes a Lead — no content filtering, no
confidence threshold, no junk detection. What the pipeline DOES do is
unwrap the forward, so the Lead is the ORIGINAL prospect inside it rather
than the employee who pressed Forward. See docs/LEAD_INGESTION_POLICY.md.

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
from . import blocklist

log = logging.getLogger(__name__)

DEDUP_DOMAIN_WINDOW_DAYS = 30


def _internet_message_id(msg: dict) -> Optional[str]:
    return msg.get('internetMessageId') or msg.get('id')


def _thread_keys(msg):
    """Conversation and RFC-822 threading headers, in one shape."""
    try:
        from app.services import lead_intake as _li
        return _li.thread_keys(msg)
    except Exception:
        return {}


def _file_against_lead(db, decision, msg, extracted, log):
    """File an email that is not a new lead against the lead it belongs to.

    This is the difference between a classifier and a shredder. A reply,
    a forward, a quotation or a rate request all carry information about
    a live enquiry; declining to create a *lead* is not a reason to lose
    the *email*. One with no lead to attach to is recorded and left.
    """
    if not decision.lead_id:
        return None
    try:
        from app import LeadEmail, Lead
        from app.services import lead_intake as _li

        message_id = (msg.get('internetMessageId') or '').strip() or None
        if message_id and LeadEmail.query.filter_by(
                lead_id=decision.lead_id, message_id=message_id).first():
            return None                       # already on the trail

        keys = _thread_keys(msg)
        outbound = decision.klass in (_li.Klass.QUOTE, _li.Klass.RATE_SOURCING)
        row = LeadEmail(
            lead_id=decision.lead_id,
            direction='outbound' if outbound else 'inbound',
            from_addr=_li.sender(msg) or None,
            to_addr=', '.join(_li.recipients(msg, 'toRecipients')) or None,
            cc=', '.join(_li.recipients(msg, 'ccRecipients')) or None,
            subject=(msg.get('subject') or '')[:500] or None,
            body=(_li.body_text(msg) or '')[:8000] or None,
            sent_or_received_at=email_parser.received_datetime(msg),
            source='ingested',
            status='received',
            message_id=message_id,
            conversation_id=keys.get('conversation_id'),
            in_reply_to=keys.get('in_reply_to'),
            references_header=keys.get('references'),
        )
        db.session.add(row)

        # A quotation going out moves the enquiry on. Nothing else here
        # changes a stage: an inbound reply is information, not progress,
        # and advancing a lead on one would corrupt the pipeline.
        if decision.klass == _li.Klass.QUOTE:
            lead = db.session.get(Lead, decision.lead_id)
            if lead is not None and (lead.stage or '') not in (
                    'Quoted', 'Won', 'Lost', 'Not Interested'):
                lead.stage = 'Quoted'
        return row
    except Exception:
        log.exception('could not file %s against lead %s',
                      msg.get('internetMessageId'), decision.lead_id)
        return None


def process_single_message(graph, mailbox: str, msg: dict) -> dict:
    """Process ONE Graph message into a Lead. Idempotent — safe to call
    multiple times for the same message; only the first call creates a
    Lead. Any subsequent call returns 'skipped' with reason='already ingested'.

    'already ingested' is the ONLY reason a message is ever skipped.
    """
    # Local imports keep this module import-safe under all circumstances.
    from app import app, db, Lead, LeadAttachment, EmailEvent  # type: ignore

    imid = _internet_message_id(msg)

    with app.app_context():
        # ── Idempotency (kept — same physical email must not double-insert) ──
        if imid:
            hit = (db.session.query(Lead.id)
                   .filter(Lead.email_message_id == imid).first())
            if hit:
                return {'status': 'skipped', 'reason': 'already ingested',
                        'lead_id': hit[0], 'internet_message_id': imid}

            # ── Respect a purge ───────────────────────────────────────
            # Deleting a Lead removes the email_message_id that dedup keys
            # on, so without this a re-poll or replay resurrects every
            # newsletter the team just cleared out. The purge marks the
            # EmailEvent 'lead_deleted'; that verdict is permanent.
            purged = (db.session.query(EmailEvent.id)
                      .filter(EmailEvent.internet_message_id == imid,
                              EmailEvent.status == 'lead_deleted').first())
            if purged:
                return {'status': 'skipped',
                        'reason': 'previously purged as irrelevant',
                        'lead_id': None, 'internet_message_id': imid}

        # ── Parse + AI-extract ────────────────────────────────────────
        # CAPTURE EVERYTHING. Every message in the leads inbox becomes a
        # Lead — the inbox is the filter. parser.extract_lead unwraps the
        # forward so `extracted` describes the ORIGINAL customer message
        # rather than the employee who forwarded it; anything the parser
        # would have wanted to skip is kept as a 'triage_tag' in opp_notes
        # so the sales team can sort in the UI. Silently dropping mail =
        # losing business.
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

        # A parser skip reason is a LABEL, not a rejection — it rides along
        # on the Lead so the sales team can sort, and nothing is dropped.
        triage_tag = extracted.get('skip_reason')       # None if clean

        forwarded_by = extracted.get('forwarded_by') or msg.get('_forwarded_by')
        sender_email  = (extracted.get('email') or '').strip().lower()

        # ── Sender denylist ───────────────────────────────────────────
        # v2026-09-02 — promotions, advertisements and newsletters never
        # become leads. Checked against the ORIGINAL sender (the forward is
        # already unwrapped by this point), so a newsletter relayed in by
        # the auto-forward is blocked on its true origin, not on whoever
        # forwarded it. Editable in data/blocked_senders.txt.
        blocked = blocklist.check(sender_email)
        if blocked:
            log.info('Blocked %s [%s]', imid, blocked)
            return {'status': 'skipped', 'reason': blocked,
                    'lead_id': None, 'internet_message_id': imid}
        sender_domain = sender_email.split('@', 1)[1] if '@' in sender_email else ''

        # ── Intake classification ─────────────────────────────────────
        # This path was deliberately zero-skip: a parser reason was a
        # label, DB dedup was removed, and a second email from the same
        # customer made a second lead. That is what filled the CRM with
        # replies, forwards, our own quotations and rate requests to
        # shipping lines.
        #
        # The classifier answers a different question from the parser:
        # not "is this logistics?" but "is this a NEW enquiry?". Only one
        # of ten classes creates a lead — and nothing is dropped, because
        # everything else is filed against the lead it belongs to.
        decision = None
        try:
            from app.services import lead_intake as _li
            from app.services import lead_intake_db as _lidb
            msg_for_class = dict(msg)
            msg_for_class['_forward_resolved'] = bool(
                extracted.get('forward_resolved'))
            decision = _li.classify(msg_for_class, _lidb.build_context())
            log.info('intake %s → %s (step %s, conf %s) %s',
                     imid, decision.klass, decision.step,
                     decision.confidence, decision.reason)
        except Exception:
            log.exception('intake classification failed for %s — '
                          'creating the lead rather than losing it', imid)

        if decision is not None and not decision.creates_lead:
            try:
                _lidb.record(decision, msg)
                _file_against_lead(db, decision, msg, extracted, log)
                db.session.commit()
            except Exception:
                db.session.rollback()
                log.exception('could not file %s against lead %s',
                              imid, decision.lead_id)
            return {'status': 'skipped',
                    'reason': f'{decision.klass}: {decision.reason}',
                    'lead_id': decision.lead_id,
                    'classification': decision.klass,
                    'internet_message_id': imid}

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
            # v2026-09-02 — date the Lead by when the email ARRIVED in the
            # leads inbox, not when we happened to process it. The portal
            # sorts on created_at, so a backfill or a replay would otherwise
            # bunch old mail at the top under today's timestamp.
            received = email_parser.received_datetime(msg) or datetime.utcnow()
            _keys = _thread_keys(msg)
            lead = Lead(
                source            = 'email',
                stage             = 'New Opportunity',
                email_message_id  = imid,
                created_at        = received,
                onboarded_date    = received.date(),
                conversation_id   = _keys.get('conversation_id'),
                in_reply_to       = _keys.get('in_reply_to'),
                references_header = _keys.get('references'),
                classification    = (decision.klass if decision else None),
                lead_confidence   = (decision.confidence if decision else None),
                duplicate_score   = (decision.duplicate_score if decision else None),
                **lead_kwargs,
            )
            db.session.add(lead)
            db.session.flush()

            # Row 1 of the email trail.
            try:
                from email_ingest.trail import record_inbound
                record_inbound(db, lead, received_at=received,
                               message_id=imid)
            except Exception:
                log.exception('email trail seed failed for lead %s', lead.id)

            # ── Attachments ─────────────────────────────────────────────
            try:
                attachments_mod.save_attachments_for_lead(
                    graph, mailbox=mailbox,
                    message_id=msg.get('id'), lead_id=lead.id,
                )
            except Exception:
                log.exception('attachments save failed for lead %s', lead.id)

            # The created leads are the labels most worth having: they
            # are the ones a human might reject.
            if decision is not None:
                try:
                    from app.services import lead_intake_db as _lidb2
                    _lidb2.record(decision, msg, created_lead_id=lead.id)
                except Exception:
                    log.exception('could not record the classification for '
                                  'lead %s', lead.id)

            db.session.commit()
            return {'status': 'created', 'lead_id': lead.id,
                    'internet_message_id': imid, 'reason': None}
        except Exception as e:
            db.session.rollback()
            log.exception('lead insert failed for %s', imid)
            return {'status': 'failed', 'reason': f'lead insert: {e}',
                    'lead_id': None, 'internet_message_id': imid}
