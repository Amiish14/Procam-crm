"""End-to-end ingest: Microsoft Graph → parser → Lead rows.

The pipeline is:
    1. Fetch messages from the shared mailbox received in the lookback window
    2. Skip if the message is already ingested (dedup by internetMessageId)
    3. Parse (unwrapping any forward); skip if parser flags a skip_reason
    4. Insert a Lead row with source='email', stage='New Opportunity'
    5. Optionally mark the message as read via Graph
    6. Return stats

Designed to be idempotent — running twice the same day never creates
duplicates.
"""
from __future__ import annotations

import json
import logging
import os
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Optional

from . import parser as email_parser
from . import ai_extractor
from .graph_client import GraphClient

log = logging.getLogger(__name__)


def run_ingest(lookback_hours: int = 26, dry_run: bool = False) -> dict:
    # v2026-08 — respect EMAIL_INGESTION_MODE. In mailbox mode the
    # webhook drives ingestion and this polled path becomes a no-op
    # so both routes never race on the same message.
    from . import service as _mail_service  # local import
    if not _mail_service.poll_should_run():
        return {"scanned": 0, "created": 0, "skipped": 0, "errors": 0,
                "note": f"mode={_mail_service.current_mode()} — poll disabled"}

    """Run one pass of the email → lead ingest.

    Args:
        lookback_hours: how many hours back to scan (default 26 = 24h + 2h overlap).
        dry_run: if True, no DB writes and no mark_as_read calls.

    Returns a stats dict.
    """
    # Local imports to avoid circular reference at module import time
    from app import app, db, Lead, LeadAttachment  # noqa: WPS433

    started = datetime.utcnow()
    stats = {
        "scanned": 0,
        "created": 0,
        "skipped": 0,
        "errors": 0,
        "skip_reasons": {},
        "started_at": started.isoformat() + "Z",
        "ended_at": None,
        "duration_s": 0.0,
        "dry_run": dry_run,
    }
    skip_counter: Counter = Counter()

    mailbox = os.environ.get("EMAIL_INGEST_MAILBOX")
    if not mailbox:
        raise RuntimeError("EMAIL_INGEST_MAILBOX env var is required.")

    # v2026-09-01 — hard lockdown: leads@procamgroup.in is the ONLY
    # sanctioned source of leads. If a one-off backfill is attempted
    # against a different mailbox (typo, forgotten env from an old
    # deploy, an admin trying to import a personal inbox), refuse to
    # run rather than silently seeding leads from the wrong stream.
    from . import service as _mail_service
    expected = (_mail_service.crm_inbox_email() or '').strip().lower()
    if expected and mailbox.strip().lower() != expected:
        raise RuntimeError(
            f"EMAIL_INGEST_MAILBOX ({mailbox!r}) does not match "
            f"CRM_INBOX_EMAIL ({expected!r}). Only the sanctioned leads "
            f"mailbox is allowed as a source. Refusing to ingest."
        )

    mark_as_read = (
        os.environ.get("EMAIL_INGEST_MARK_AS_READ", "false").lower() == "true"
    )

    with app.app_context():
        since_utc = datetime.utcnow() - timedelta(hours=lookback_hours)
        log.info(
            "Email ingest starting: mailbox=%s since=%sZ lookback_hours=%s dry_run=%s",
            mailbox, since_utc.isoformat(), lookback_hours, dry_run,
        )

        try:
            graph = GraphClient()
        except RuntimeError as e:
            log.error("GraphClient init failed: %s", e)
            stats["errors"] += 1
            stats["ended_at"] = datetime.utcnow().isoformat() + "Z"
            stats["duration_s"] = (datetime.utcnow() - started).total_seconds()
            stats["fatal"] = str(e)
            return stats

        pending_since_commit = 0
        today = date.today()

        # ── Per-run dedup ────────────────────────────────────────────────
        # (a) one lead per sender email address — highest confidence wins
        # (b) one lead per sender DOMAIN per run — stops 30 emails from
        #     sales1@wsdlogistics.com, sales2@wsdlogistics.com etc. all
        #     landing as separate leads on the same day
        seen_email_in_run: dict = {}   # email_lower → confidence
        seen_domain_in_run: dict = {}  # domain_lower → (confidence, email_lower)

        # ── DB-existing dedup ────────────────────────────────────────────
        # (a) exact email match — always skip (this person is already a lead)
        # (b) same domain within the last N days — skip (avoids creating
        #     new leads for the same company that already contacted us
        #     recently, e.g. via a different person in the same team)
        existing_emails = set()
        existing_recent_domains = set()
        DEDUP_DOMAIN_WINDOW_DAYS = 30
        try:
            for (e,) in db.session.query(Lead.email).filter(
                Lead.email.isnot(None), Lead.email != ""
            ).all():
                if e:
                    existing_emails.add(e.strip().lower())

            cutoff = datetime.utcnow() - timedelta(days=DEDUP_DOMAIN_WINDOW_DAYS)
            for (e,) in db.session.query(Lead.email).filter(
                Lead.email.isnot(None), Lead.email != "",
                Lead.created_at >= cutoff,
            ).all():
                if e and "@" in e:
                    d = e.split("@", 1)[1].strip().lower()
                    if d:
                        existing_recent_domains.add(d)
        except Exception:
            log.exception("Could not preload existing lead emails; proceeding without DB dedup")

        try:
            iterator = graph.list_messages(mailbox=mailbox, since_utc=since_utc, top=100)
        except Exception as e:  # noqa: BLE001
            log.exception("Failed to start Graph listing: %s", e)
            stats["errors"] += 1
            stats["ended_at"] = datetime.utcnow().isoformat() + "Z"
            stats["duration_s"] = (datetime.utcnow() - started).total_seconds()
            stats["fatal"] = str(e)
            return stats

        for msg in iterator:
            stats["scanned"] += 1
            msg_id_graph = msg.get("id")
            msg_id_internet: Optional[str] = msg.get("internetMessageId")

            try:
                # Idempotency: skip if we've seen this internetMessageId
                if msg_id_internet:
                    exists = (
                        db.session.query(Lead.id)
                        .filter(Lead.email_message_id == msg_id_internet)
                        .first()
                    )
                    if exists:
                        stats["skipped"] += 1
                        skip_counter["already ingested"] += 1
                        continue

                extracted = email_parser.extract_lead(msg)
                if extracted is None:
                    stats["skipped"] += 1
                    skip_counter["parser returned None"] += 1
                    continue

                if extracted.get("skip_reason"):
                    reason = extracted["skip_reason"]
                    stats["skipped"] += 1
                    skip_counter[reason] += 1
                    log.debug(
                        "Skip [%s] subject=%r reason=%s",
                        msg_id_internet, extracted.get("subject", "")[:80], reason,
                    )
                    continue

                received_at = email_parser.received_datetime(msg)
                sender_email = (extracted.get("email") or "").strip().lower()
                sender_domain = sender_email.split("@", 1)[1] if "@" in sender_email else ""
                confidence = float(extracted.get("confidence") or 0.0)

                # DB dedup 1: exact email already in Leads → skip
                if sender_email and sender_email in existing_emails:
                    stats["skipped"] += 1
                    skip_counter["existing lead: same email"] += 1
                    continue

                # DB dedup 2: same domain has an existing lead in the last
                # 30 days → skip (avoids duplicating a company that already
                # contacted us via a different person recently).
                if sender_domain and sender_domain in existing_recent_domains:
                    stats["skipped"] += 1
                    skip_counter[f"existing lead: same domain <{DEDUP_DOMAIN_WINDOW_DAYS}d"] += 1
                    continue

                # Per-run dedup 1: same sender email seen twice in this run
                if sender_email and sender_email in seen_email_in_run:
                    prev_conf = seen_email_in_run[sender_email]
                    if confidence <= prev_conf:
                        stats["skipped"] += 1
                        skip_counter["dup sender in this run"] += 1
                        continue
                    stats["created"] -= 1   # supersede the earlier queued lead
                    skip_counter["dup sender: superseded by stronger msg"] += 1
                seen_email_in_run[sender_email] = max(
                    confidence, seen_email_in_run.get(sender_email, 0.0)
                )

                # Per-run dedup 2: same sender DOMAIN seen twice in this run
                # → skip anything after the first, unless the new one has
                # materially higher confidence (>+0.15) AND is a different
                # person (different local-part).
                if sender_domain and sender_domain in seen_domain_in_run:
                    prev_conf, prev_email = seen_domain_in_run[sender_domain]
                    same_person = (sender_email == prev_email)
                    much_stronger = (confidence >= prev_conf + 0.15)
                    if same_person or not much_stronger:
                        stats["skipped"] += 1
                        skip_counter["dup domain in this run"] += 1
                        continue
                    stats["created"] -= 1   # supersede the earlier queued lead
                    skip_counter["dup domain: superseded by stronger msg"] += 1
                seen_domain_in_run[sender_domain] = (confidence, sender_email)

                # AI extraction (opt-in, only if ANTHROPIC_API_KEY is set).
                # Merges on top of regex — AI fields win where both have data.
                ai_data = None
                if ai_extractor.is_enabled():
                    try:
                        ai_data = ai_extractor.extract(msg, extracted)
                    except Exception as e:  # noqa: BLE001
                        log.warning("AI extraction failed for %s: %s", sender_email, e)
                        ai_data = None

                # AI classifier: skip anything that isn't a genuine new-business lead
                # (vendor pitches, ops emails from existing customers, banking, newsletters).
                if ai_data and ai_data.get("is_business_lead") is False:
                    lead_type = ai_data.get("lead_type") or "not a lead"
                    reject = ai_data.get("reject_reason") or "AI classified as non-lead"
                    stats["skipped"] += 1
                    skip_counter[f"AI: {lead_type}"] += 1
                    log.info("Skip [%s] %s | %s", sender_email, lead_type, reject[:80])
                    continue

                # Merge AI over regex (AI wins where both have values)
                merged = dict(
                    company=extracted.get("company") or "Unknown",
                    contact_name=extracted.get("contact_name") or "",
                    designation="",
                    phone_primary=extracted.get("phone") or None,
                    phone_secondary=None,
                    email_primary=extracted.get("email") or "",
                    email_secondary=None,
                    origin=(extracted.get("signals", {}) or {}).get("origin"),
                    destination=(extracted.get("signals", {}) or {}).get("destination"),
                    cargo_type=None,
                    cargo_weight_mt=None,
                    cargo_dimensions=None,
                    cargo_qty=None,
                    procam_vertical=None,
                    requirement_type=("RFQ" if (extracted.get("signals", {}) or {}).get("rfq") else None),
                    urgency=(extracted.get("signals", {}) or {}).get("urgency"),
                    target_date=None,
                    special_requirements=[],
                    one_line_summary="",
                    next_action_suggested=None,
                )
                if ai_data:
                    for k, v in ai_data.items():
                        if v not in (None, "", []):
                            merged[k] = v

                # AUTHORITATIVE OVERRIDES — Graph headers win over anything AI hallucinates
                # from the quoted-reply body. Prevents cases where AI reads an ancient
                # quoted message deep in the thread and mis-attributes the sender.
                if sender_email:
                    merged["email_primary"] = sender_email
                    # Derive company from the sender's actual domain unless AI's company
                    # is clearly aligned with it (starts with same 4+ char prefix).
                    from email_ingest.parser import _company_from_domain
                    dom_company = _company_from_domain(sender_email) or ""
                    ai_company = (merged.get("company") or "").strip()
                    from email_ingest.parser import _domain_root
                    stem = _domain_root(sender_email)
                    if dom_company and (not ai_company or
                                        not ai_company.lower().startswith(stem[:4])):
                        merged["company"] = dom_company

                # Guard against AI inventing a phone that isn't in the email body —
                # if regex found no phone in the actual email, drop AI's phone_primary.
                if not (extracted.get("phone")) and not (
                    ((extracted.get("body_text") or "").lower()).replace(" ", "").find(
                        (merged.get("phone_primary") or "").replace("+91-", "").replace("-", "")
                    ) >= 0 if merged.get("phone_primary") else False
                ):
                    merged["phone_primary"] = None

                # Guaranteed-populated fallbacks (never blank in the UI card):
                if not merged.get("one_line_summary"):
                    subj = (extracted.get("subject") or "").strip()
                    cargo_hint = ", ".join(
                        (extracted.get("signals", {}) or {}).get("cargo_keywords", [])[:3]
                    )
                    parts = []
                    if merged.get("requirement_type"):
                        parts.append(merged["requirement_type"])
                    elif (extracted.get("signals", {}) or {}).get("rfq"):
                        parts.append("RFQ")
                    if cargo_hint:
                        parts.append(f"({cargo_hint})")
                    if merged.get("origin") and merged.get("destination"):
                        parts.append(f"{merged['origin']} → {merged['destination']}")
                    prefix = " ".join(parts).strip()
                    merged["one_line_summary"] = (
                        f"{prefix}: {subj}"[:140] if prefix else subj[:140] or
                        f"Inbound email from {merged.get('company','Unknown')}"
                    )

                if not merged.get("procam_vertical"):
                    ck = [c.lower() for c in
                          (extracted.get("signals", {}) or {}).get("cargo_keywords", [])]
                    if any(k in ck for k in ("odc", "over-dimensional", "over dimensional",
                                             "heavy lift", "hydraulic", "trailer")):
                        merged["procam_vertical"] = "Heavy Cargo"
                    elif any(k in ck for k in ("project cargo",)):
                        merged["procam_vertical"] = "Project Freight"
                    elif any(k in ck for k in ("container", "containers", "freight",
                                               "shipment", "consignment")):
                        merged["procam_vertical"] = "Freight Forwarding"
                    elif any(k in ck for k in ("warehouse", "warehousing", "storage")):
                        merged["procam_vertical"] = "Warehousing"
                    elif any(k in ck for k in ("installation", "rigging")):
                        merged["procam_vertical"] = "Installation"
                    else:
                        merged["procam_vertical"] = "General Transport"

                if not merged.get("next_action_suggested"):
                    if merged.get("requirement_type") == "RFQ":
                        merged["next_action_suggested"] = "Reply with a quote within 24h"
                    elif merged.get("urgency") in ("High", "high"):
                        merged["next_action_suggested"] = "Call the contact today to qualify"
                    else:
                        merged["next_action_suggested"] = "Reply to acknowledge and qualify budget + timeline"

                # Normalise urgency casing (AI sometimes returns lowercase)
                u = merged.get("urgency")
                if isinstance(u, str) and u:
                    merged["urgency"] = u.capitalize()

                # Build Lead row — field names verified against app.py::Lead
                lead = Lead(
                    source="email",
                    stage="New Opportunity",
                    company=(merged["company"] or "Unknown")[:200],
                    pic=(merged["contact_name"] or "")[:100],
                    designation_pic=(merged["designation"] or "")[:100],
                    email=(merged["email_primary"] or "")[:120],
                    phone=(merged["phone_primary"] or None),
                    email2=(merged["email_secondary"] or None),
                    phone2=(merged["phone_secondary"] or None),
                    procam_vertical=merged["procam_vertical"],
                    notes=(extracted.get("body_text") or "")[:8000],
                    opp_notes=json.dumps({
                        "signals": extracted.get("signals", {}),
                        "confidence": extracted.get("confidence", 0.0),
                        "source_subject": extracted.get("subject", ""),
                        "ai_used": bool(ai_data),
                    }),
                    email_message_id=msg_id_internet,
                    email_extracted_json=json.dumps(merged, default=str),
                    onboarded_date=(received_at.date() if received_at else today),
                    created_at=(received_at or datetime.utcnow()),
                )

                if dry_run:
                    log.info(
                        "[dry-run] Would create Lead: company=%r pic=%r email=%s "
                        "vertical=%s route=%s→%s cargo=%s urgency=%s | %s",
                        lead.company, lead.pic, lead.email,
                        merged.get("procam_vertical"),
                        merged.get("origin"), merged.get("destination"),
                        merged.get("cargo_type"),
                        merged.get("urgency"),
                        (merged.get("one_line_summary") or "")[:100],
                    )
                    stats["created"] += 1
                    continue

                db.session.add(lead)

                # Flush to assign lead.id so we can key attachments to it.
                # Rolled-back on failure so the outer batch keeps going.
                try:
                    db.session.flush()
                except Exception as flush_err:  # noqa: BLE001
                    log.exception("Flush failed before attachment fetch: %s", flush_err)
                    db.session.rollback()
                    stats["errors"] += 1
                    pending_since_commit = 0
                    continue

                stats["created"] += 1
                pending_since_commit += 1

                # Attachment fetch — only if Graph flagged the message as
                # having attachments (avoids a wasted API call per lead).
                if msg.get("hasAttachments") and lead.id:
                    from email_ingest.attachments import save_attachments_for_lead
                    try:
                        saved_atts = save_attachments_for_lead(
                            graph=graph,
                            mailbox=mailbox,
                            message_id=msg_id_graph,
                            lead_id=lead.id,
                        )
                    except Exception:
                        log.exception(
                            "save_attachments_for_lead failed lead=%s msg=%s",
                            lead.id, msg_id_graph,
                        )
                        saved_atts = []

                    for meta in saved_atts:
                        try:
                            att = LeadAttachment(
                                lead_id=lead.id,
                                filename=meta["filename"],
                                content_type=meta["content_type"],
                                size_bytes=meta["size_bytes"],
                                storage_path=meta["storage_path"],
                                source="email",
                                email_attachment_id=meta.get("email_attachment_id") or None,
                            )
                            db.session.add(att)
                        except Exception:
                            log.exception("Failed to add LeadAttachment row lead=%s", lead.id)

                if pending_since_commit >= 20:
                    db.session.commit()
                    pending_since_commit = 0

                if mark_as_read and msg_id_graph:
                    graph.mark_as_read(mailbox, msg_id_graph)

            except Exception as e:  # noqa: BLE001
                # One bad message must not kill the batch
                log.exception(
                    "Error processing message id=%s internetMessageId=%s: %s",
                    msg_id_graph, msg_id_internet, e,
                )
                stats["errors"] += 1
                try:
                    db.session.rollback()
                except Exception:
                    pass
                pending_since_commit = 0
                continue

        # Final flush
        if not dry_run and pending_since_commit > 0:
            try:
                db.session.commit()
            except Exception as e:  # noqa: BLE001
                log.exception("Final commit failed: %s", e)
                stats["errors"] += 1
                try:
                    db.session.rollback()
                except Exception:
                    pass

    ended = datetime.utcnow()
    stats["ended_at"] = ended.isoformat() + "Z"
    stats["duration_s"] = round((ended - started).total_seconds(), 3)
    stats["skip_reasons"] = dict(skip_counter)

    log.info(
        "Email ingest done: scanned=%s created=%s skipped=%s errors=%s duration=%ss",
        stats["scanned"], stats["created"], stats["skipped"],
        stats["errors"], stats["duration_s"],
    )
    return stats
