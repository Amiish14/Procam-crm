"""End-to-end ingest: Microsoft Graph → parser → Lead rows.

The pipeline is:
    1. Fetch messages from the shared mailbox received in the lookback window
    2. Skip if the message is already ingested (dedup by internetMessageId)
    3. Parse; skip if parser flags a skip_reason
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
from .graph_client import GraphClient

log = logging.getLogger(__name__)


def run_ingest(lookback_hours: int = 26, dry_run: bool = False) -> dict:
    """Run one pass of the email → lead ingest.

    Args:
        lookback_hours: how many hours back to scan (default 26 = 24h + 2h overlap).
        dry_run: if True, no DB writes and no mark_as_read calls.

    Returns a stats dict.
    """
    # Local imports to avoid circular reference at module import time
    from app import app, db, Lead  # noqa: WPS433

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

                # Build Lead row — field names verified against app.py::Lead
                lead = Lead(
                    source="email",
                    stage="New Opportunity",
                    company=(extracted.get("company") or "Unknown")[:200],
                    pic=(extracted.get("contact_name") or "")[:100],
                    email=(extracted.get("email") or "")[:120],
                    phone=(extracted.get("phone") or None),
                    notes=(extracted.get("body_text") or "")[:8000],
                    opp_notes=json.dumps({
                        "signals": extracted.get("signals", {}),
                        "confidence": extracted.get("confidence", 0.0),
                        "source_subject": extracted.get("subject", ""),
                    }),
                    email_message_id=msg_id_internet,
                    onboarded_date=today,
                    created_at=datetime.utcnow(),
                )

                if dry_run:
                    log.info(
                        "[dry-run] Would create Lead: company=%r email=%s "
                        "confidence=%s cargo=%s",
                        lead.company, lead.email,
                        extracted.get("confidence"),
                        extracted.get("signals", {}).get("cargo_keywords"),
                    )
                    stats["created"] += 1
                    continue

                db.session.add(lead)
                stats["created"] += 1
                pending_since_commit += 1

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
