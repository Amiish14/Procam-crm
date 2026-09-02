"""
v2026-08 — Shared "AI-merge + defaults" step used by BOTH the poll pipeline
and the mailbox webhook. Produces the same rich Lead payload for either
ingest route so the "LEAD SUMMARY · FROM INBOUND EMAIL" card in the CRM
renders identically regardless of how the email arrived.

Public entry: build_enriched_lead_kwargs(msg, extracted, *, sender_email,
                                        sender_domain, forwarded_by=None)

Returns a dict of kwargs to splat into `Lead(...)`. The caller is responsible
for adding `source`, `stage`, `email_message_id`, and `created_at`.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from . import ai_router as ai_extractor    # router prefers Groq, falls back to Anthropic
from . import parser as email_parser

log = logging.getLogger(__name__)


def _merge_ai(extracted: dict, msg: dict) -> tuple[dict, Optional[dict]]:
    """Run the AI extractor (if enabled) and merge results over the regex
    baseline. Returns (merged, ai_data). ai_data is None when AI is off or
    fails."""
    ai_data = None
    if ai_extractor.is_enabled():
        try:
            ai_data = ai_extractor.extract(msg, extracted)
        except Exception as e:                                        # noqa: BLE001
            log.warning("AI extraction failed: %s", e)
            ai_data = None

    signals = extracted.get("signals", {}) or {}
    merged = dict(
        company              = extracted.get("company") or "Unknown",
        contact_name         = extracted.get("contact_name") or "",
        designation          = "",
        phone_primary        = extracted.get("phone") or None,
        phone_secondary      = None,
        email_primary        = extracted.get("email") or "",
        email_secondary      = None,
        origin               = signals.get("origin"),
        destination          = signals.get("destination"),
        cargo_type           = None,
        cargo_weight_mt      = None,
        cargo_dimensions     = None,
        cargo_qty            = None,
        procam_vertical      = None,
        requirement_type     = ("RFQ" if signals.get("rfq") else None),
        urgency               = signals.get("urgency"),
        target_date          = None,
        special_requirements = [],
        one_line_summary     = "",
        next_action_suggested= None,
        is_business_lead     = True,
        # v2026-08 — classification + validation additions
        classification       = None,
        classification_source= None,
        employee_note        = None,
        needs_review         = False,
        confidence           = None,
        _ai_model            = None,
    )
    if ai_data:
        for k, v in ai_data.items():
            if v not in (None, "", []):
                merged[k] = v
    return merged, ai_data


def _apply_overrides_and_defaults(merged: dict, extracted: dict,
                                  sender_email: str,
                                  forwarded_by: Optional[dict] = None) -> dict:
    """Authoritative overrides + guaranteed-populated defaults so the summary
    card is never blank."""
    # v2026-08 — never let an internal Procam address masquerade as the
    # customer email. If the immediate sender is on our domain the parser
    # should have promoted a customer sender from the body; if it couldn't,
    # leave email_primary as-is (may be null) and flag needs_review.
    _internal = sender_email and (sender_email.lower().endswith('@procamgroup.in')
                                  or sender_email.lower().endswith('@procamlogistics.com'))
    if sender_email and not _internal:
        merged["email_primary"] = sender_email
        dom_company = email_parser._company_from_domain(sender_email) or ""
        ai_company  = (merged.get("company") or "").strip()
        stem = (sender_email.split("@", 1)[1].split(".")[0] or "").lower()
        if dom_company and (not ai_company
                            or not ai_company.lower().startswith(stem[:4])):
            merged["company"] = dom_company
    elif _internal:
        # The lead is captured either way — but a Procam address must never
        # sit in the contact field. Blank it and flag for review so the
        # sales team fills in the real prospect from the body.
        merged["email_primary"] = None
        merged["needs_review"] = True
        merged["extraction_notes"] = ((merged.get("extraction_notes") or '') +
            ' | Immediate sender is a Procam address; could not identify external customer.').strip(' |')

    # v2026-09 — the employee who forwarded the mail is a *courier*, never
    # the lead. If the AI (or a signature block) managed to put their
    # address or name on the contact fields, strip it back out.
    fwd_email = ((forwarded_by or {}).get("email") or "").strip().lower()
    fwd_name  = ((forwarded_by or {}).get("name") or "").strip().lower()
    if fwd_email:
        for field in ("email_primary", "email_secondary"):
            if (merged.get(field) or "").strip().lower() == fwd_email:
                merged[field] = None
                merged["needs_review"] = True
        if fwd_name and (merged.get("contact_name") or "").strip().lower() == fwd_name:
            merged["contact_name"] = ""
        # Company derived from the forwarder's own domain is equally wrong.
        fwd_company = (email_parser._company_from_domain(fwd_email) or "").lower()
        if fwd_company and (merged.get("company") or "").strip().lower() == fwd_company:
            merged["company"] = ""

    # Drop AI-hallucinated phones that don't actually appear in the body.
    body_norm = ((extracted.get("body_text") or "").lower()).replace(" ", "")
    phone_norm = (merged.get("phone_primary") or "")\
        .replace("+91-", "").replace("-", "")
    if not extracted.get("phone") and phone_norm and phone_norm not in body_norm:
        merged["phone_primary"] = None

    # One-line summary
    if not merged.get("one_line_summary"):
        subj = (extracted.get("subject") or "").strip()
        cargo_hint = ", ".join(
            (extracted.get("signals", {}) or {}).get("cargo_keywords", [])[:3])
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
            f"{prefix}: {subj}"[:140] if prefix else subj[:140]
            or f"Inbound email from {merged.get('company','Unknown')}"
        )

    # Procam vertical fallback
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

    # Suggested next action fallback
    if not merged.get("next_action_suggested"):
        if merged.get("requirement_type") == "RFQ":
            merged["next_action_suggested"] = "Reply with a quote within 24h"
        elif str(merged.get("urgency") or "").lower() == "high":
            merged["next_action_suggested"] = "Call the contact today to qualify"
        else:
            merged["next_action_suggested"] = (
                "Reply to acknowledge and qualify budget + timeline")

    # Normalise urgency casing (AI sometimes returns lowercase)
    u = merged.get("urgency")
    if isinstance(u, str) and u:
        merged["urgency"] = u.capitalize()

    return merged


def build_enriched_lead_kwargs(msg: dict, extracted: dict, *,
                               sender_email: str, sender_domain: str,
                               forwarded_by: Optional[dict] = None) -> dict:
    """Full enrichment step. Returns a dict ready to splat into `Lead(...)`.

    Caller is responsible for adding source, stage, email_message_id, and
    created_at fields.
    """
    merged, ai_data = _merge_ai(extracted, msg)
    merged = _apply_overrides_and_defaults(merged, extracted, sender_email,
                                           forwarded_by=forwarded_by)

    # Notes body — the ORIGINAL customer message. The forwarding
    # employee's covering note is kept above it, clearly labelled, so the
    # sales team can see who relayed the lead and what they said without
    # that text ever being mistaken for the customer's own words.
    body_notes = (extracted.get("body_text") or "")[:8000]
    if forwarded_by:
        from datetime import datetime
        who = (forwarded_by.get("email") if isinstance(forwarded_by, dict)
               else str(forwarded_by))
        header = (f"[Forwarded to CRM by {who} on "
                  f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}]")
        note = (extracted.get("forward_note") or "").strip()
        if note:
            header += f"\n[Their note: {note[:500]}]"
        body_notes = f"{header}\n\n--- Original message ---\n{body_notes}"

    return {
        "company":              (merged["company"] or "Unknown")[:200],
        "pic":                  (merged["contact_name"] or "")[:100],
        "designation_pic":      (merged["designation"] or "")[:100],
        "email":                (merged["email_primary"] or "")[:120] or None,
        "phone":                merged["phone_primary"] or None,
        "email2":               merged["email_secondary"] or None,
        "phone2":               merged["phone_secondary"] or None,
        "procam_vertical":      merged["procam_vertical"],
        "notes":                body_notes,
        "opp_notes": json.dumps({
            "signals":         extracted.get("signals", {}),
            "confidence":      extracted.get("confidence", 0.0),
            "source_subject":  extracted.get("subject", ""),
            "ai_used":         bool(ai_data),
            "forwarded_by":    forwarded_by,
            "forward_note":    (extracted.get("forward_note") or "")[:500] or None,
            "original_subject": extracted.get("subject", ""),
            "outer_subject":   extracted.get("outer_subject", ""),
        }, default=str),
        "email_extracted_json": json.dumps(merged, default=str),
    }
