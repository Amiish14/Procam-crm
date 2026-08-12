"""AI extractor — sends email body to Claude Haiku, returns structured JSON.

Opt-in: only fires if ANTHROPIC_API_KEY is set in the environment.
Model default: claude-haiku-4-5-20251001 (cheapest capable model).
Override via EMAIL_INGEST_AI_MODEL env var.

Cost estimate at $0.80/M input, $4/M output, ~1000 in + 300 out tokens per
email: ~$0.002/email. At 245 emails/day: ~$15/month.

The extractor:
  1. Builds a strict JSON schema prompt.
  2. Calls Claude with the email content.
  3. Parses/validates the returned JSON.
  4. Returns a dict of fields; None if AI unavailable/failed.

The caller (pipeline) MERGES the AI result on top of the regex result —
AI wins where both have a value, regex fills gaps AI missed.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

log = logging.getLogger(__name__)

# Reasonable defaults; both overrideable via env
_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_MAX_BODY_CHARS = 6000    # cap input to control cost; email tails are usually signatures/quoted-thread noise

# JSON schema we ask Claude to emit. Kept flat/simple so bad JSON is rarer.
_SCHEMA_DOC = """{
  "is_business_lead": "boolean — TRUE only if this is a genuine new-business inquiry where the sender (or their company) wants Procam to provide logistics/transport/warehousing services. See RULES below.",
  "lead_type": "one of [inbound_rfq, prospect_inquiry, vendor_pitch, existing_customer_ops, banking_or_admin, newsletter_or_marketing, personal_or_other]",
  "reject_reason": "string or null — if is_business_lead is false, one short line explaining why (e.g. 'freight forwarder selling us services', 'operational update on existing shipment', 'bank statement')",
  "company": "string or null (registered/trading name of the sender's company)",
  "contact_name": "string or null (person writing the email)",
  "designation": "string or null (e.g. 'Sr Manager - Logistics')",
  "phone_primary": "string or null (with country code if known, e.g. +91-9876543210)",
  "phone_secondary": "string or null",
  "email_primary": "string or null",
  "email_secondary": "string or null",
  "origin": "string or null (pickup city + state, e.g. 'Vadodara, Gujarat')",
  "destination": "string or null (delivery city + state)",
  "cargo_type": "string or null (what is being transported, e.g. 'Steel Coils', 'Reactor Vessel')",
  "cargo_weight_mt": "number or null (weight in metric tons)",
  "cargo_dimensions": "string or null (L x W x H if given)",
  "cargo_qty": "string or null (number of pieces/containers/trucks)",
  "procam_vertical": "one of [Heavy Cargo, Project Freight, Freight Forwarding, Warehousing, Installation, General Transport, Other]",
  "requirement_type": "one of [RFQ, Enquiry, Booking, Follow-up, Existing Customer, Other]",
  "urgency": "one of [High, Medium, Low]",
  "target_date": "string or null (YYYY-MM-DD if a specific date is mentioned)",
  "special_requirements": ["array of short strings — permits, escorts, insurance, hazmat, temperature, route survey, etc."],
  "one_line_summary": "string — one sentence describing what the sender wants, max 140 chars",
  "next_action_suggested": "string — a specific next step for the sales rep"
}"""

_SYSTEM_PROMPT = """You are a strict lead classifier for Procam Group — a project cargo, ODC, freight forwarding, warehousing & installation company in India.

Your job: read each incoming email and decide whether it represents a GENUINE NEW-BUSINESS SALES LEAD — i.e. a potential customer (or their agent) asking Procam to move / warehouse / install something for them.

Return ONLY valid JSON matching the schema. No prose. No markdown. No code fences.

═════════════════════════════════════════════════════════════════════
FIRST: classify with `is_business_lead` (boolean) and `lead_type`.
═════════════════════════════════════════════════════════════════════

`is_business_lead: true` ONLY when the sender is a POTENTIAL CUSTOMER asking Procam to do logistics work for them. Examples that qualify:
  - "Please quote for moving 45 MT reactor from Vadodara to Kandla by 25th"
  - "We need warehousing space in Chennai for 5000 sqft, 6 months"
  - "Requesting rates for FTL Bhiwadi → Nhava Sheva, monthly volume 10 trucks"
  - "Enquiry: installation of 800MW transformer at Hyderabad plant"
  - Any new inquiry from a manufacturer, EPC, project developer, exporter, or trader wanting Procam's services.

`is_business_lead: false` for ALL of the following — set the matching `lead_type`:
  - `vendor_pitch` — another logistics / freight / shipping / warehouse company is trying to sell THEIR services to Procam (e.g. offering LCL space, container slots, warehouse partnership, freight forwarding tie-up, driver-app subscription, tracking SaaS)
  - `existing_customer_ops` — the sender is following up on an existing shipment / LR / PO / job (references LR#, PO#, invoice#, "status of my consignment", "POD pending", "when will vehicle reach", "eway bill expired")
  - `banking_or_admin` — bank statements, payment reminders, invoices, tax notifications, government tender alerts, GST/compliance emails
  - `newsletter_or_marketing` — industry newsletters, conference invites, webinar promotions, LinkedIn digests, product marketing
  - `personal_or_other` — anything else (job applications, spam, personal correspondence)

If unsure, err on the side of `is_business_lead: false`. Only 5-15% of a typical business inbox is genuine new leads.

═════════════════════════════════════════════════════════════════════
For `is_business_lead: true`: also set `reject_reason: null` and fully populate ALL other fields per the schema.
For `is_business_lead: false`: set `reject_reason` (one short line), and you can leave logistics-specific fields (origin, destination, cargo_type, etc.) as null. But still fill `company`, `contact_name`, `email_primary`, `one_line_summary`, `procam_vertical` (best guess or "Other"), `requirement_type` ("Other" or best fit), `urgency` ("Low"), `next_action_suggested` ("No action — not a lead").
═════════════════════════════════════════════════════════════════════

Extraction rules for the leads:

1. `one_line_summary` — ALWAYS required. One sentence max 140 chars describing what the sender wants.

2. `procam_vertical` — ALWAYS required. Best-fit from [Heavy Cargo, Project Freight, Freight Forwarding, Warehousing, Installation, General Transport, Other]. ODC / reactor / heavy lift → Heavy Cargo. Containers / FCL / LCL / port → Freight Forwarding. Turnkey plant move → Project Freight. FTL / PTL / dry cargo → General Transport.

3. `requirement_type` — [RFQ, Enquiry, Booking, Follow-up, Existing Customer, Other]. RFQ = explicit quote request. Enquiry = general question. Booking = ready to execute.

4. `urgency` — [High, Medium, Low]. "urgent"/"ASAP" or ≤7-day deadline → High. ≤30-day → Medium.

5. `next_action_suggested` — always concrete. For non-leads use "No action — not a lead".

6. `special_requirements` — always an array (possibly empty). Tags: "over-dimensional permit", "escort vehicle", "hazmat", "temperature-controlled", "route survey", "night movement", "cranes at loading".

7. Numeric fields (cargo_weight_mt) — extract only if clearly stated.

Do not invent facts. Do not include fields outside the schema."""

_USER_PROMPT_TEMPLATE = """Schema:
{schema}

Email details:
Subject: {subject}
From: {from_name} <{from_email}>
Date: {date}
Body:
---
{body}
---

Return the JSON now."""


def _clean_body(body: str, max_chars: int = _MAX_BODY_CHARS) -> str:
    """Truncate body, strip quoted-reply trails to keep prompt cost down."""
    if not body:
        return ""
    body = body.strip()
    # Cut off common quoted-reply markers so we don't send old thread history
    cut_markers = [
        "\n-----Original Message-----",
        "\nFrom: ",
        "\nOn ",
        "\n________________________________",
    ]
    for m in cut_markers:
        idx = body.find(m, 200)   # only cut if marker appears past first 200 chars
        if idx != -1:
            body = body[:idx]
    if len(body) > max_chars:
        body = body[:max_chars] + "\n[truncated]"
    return body


def _repair_json(text: str) -> Optional[str]:
    """Best-effort strip of code fences / prose around the JSON block."""
    if not text:
        return None
    # Strip common code-fence wrappers
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    # Grab the outermost {…}
    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    return text[first : last + 1]


def is_enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def extract(msg: dict, regex_result: dict) -> Optional[dict]:
    """Call Claude Haiku with the email; return structured dict or None.

    `msg` is the raw Graph message dict.
    `regex_result` is the dict returned by parser.extract_lead — we don't
    strictly need it but we pass it in case we want to short-circuit on
    high-regex-confidence emails in the future.
    """
    if not is_enabled():
        return None

    try:
        import anthropic
    except Exception as e:  # noqa: BLE001
        log.warning("anthropic SDK not importable: %s", e)
        return None

    model = os.environ.get("EMAIL_INGEST_AI_MODEL", _DEFAULT_MODEL)

    from_addr = ""
    from_name = ""
    try:
        f = msg.get("from") or {}
        ea = f.get("emailAddress") or {}
        from_addr = ea.get("address") or ""
        from_name = ea.get("name") or ""
    except Exception:
        pass

    body = (msg.get("body") or {}).get("content") or msg.get("bodyPreview") or ""
    body = regex_result.get("body_text") or body   # prefer already-stripped text
    body = _clean_body(body)
    subject = (msg.get("subject") or "").strip()
    date = msg.get("receivedDateTime") or ""

    user_prompt = _USER_PROMPT_TEMPLATE.format(
        schema=_SCHEMA_DOC,
        subject=subject or "(no subject)",
        from_name=from_name or "(no name)",
        from_email=from_addr or "(no email)",
        date=date,
        body=body or "(empty body)",
    )

    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model=model,
            max_tokens=800,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw_text = ""
        for block in (resp.content or []):
            if getattr(block, "type", "") == "text":
                raw_text += block.text
    except Exception as e:  # noqa: BLE001
        log.warning("AI call failed for %s: %s", from_addr, e)
        return None

    json_str = _repair_json(raw_text)
    if not json_str:
        log.warning("AI returned unparseable output for %s: %r", from_addr, raw_text[:200])
        return None

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        log.warning("AI JSON decode failed for %s: %s  raw=%r", from_addr, e, json_str[:200])
        return None

    if not isinstance(data, dict):
        return None

    # Normalize types + strip nonsense
    for k in ("cargo_weight_mt",):
        v = data.get(k)
        if v not in (None, ""):
            try:
                data[k] = float(v)
            except (TypeError, ValueError):
                data[k] = None
    sr = data.get("special_requirements")
    if isinstance(sr, str):
        data["special_requirements"] = [sr]
    elif not isinstance(sr, list):
        data["special_requirements"] = []

    return data
