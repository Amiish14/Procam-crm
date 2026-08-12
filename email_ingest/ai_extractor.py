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
  "procam_vertical": "one of [Heavy Cargo, Project Freight, Freight Forwarding, Warehousing, Installation, General Transport, Other] or null",
  "requirement_type": "one of [RFQ, Enquiry, Booking, Follow-up, Existing Customer, Other] or null",
  "urgency": "one of [High, Medium, Low] or null",
  "target_date": "string or null (YYYY-MM-DD if a specific date is mentioned)",
  "special_requirements": ["array of short strings — permits, escorts, insurance, hazmat, temperature, route survey, etc."],
  "one_line_summary": "string — one sentence describing what the sender wants, max 140 chars",
  "next_action_suggested": "string or null — a specific next step for the recruiter/sales rep"
}"""

_SYSTEM_PROMPT = """You are extracting logistics/transport lead information from inbound business emails at Procam Group, a project cargo and freight forwarding company in India.

Return ONLY valid JSON matching the schema. No prose, no markdown, no code fences.
If a field is unknown, use null (not empty string). Never invent facts not present in the email.
For numeric fields, extract only if clearly stated (e.g. "45 MT" → 45).
For dates, extract only if unambiguous (e.g. "by 15th August" in 2026 → "2026-08-15").
Do not include fields that aren't in the schema."""

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
