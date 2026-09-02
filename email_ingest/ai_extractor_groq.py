"""
v2026-08 — Groq / Llama AI extractor for email → structured CRM record.

Drop-in alternative to email_ingest.ai_extractor (which uses Anthropic
Claude). Same input, same output shape.

Env:
    GROQ_API_KEY              — required
    EMAIL_AI_MODEL_GROQ       — optional, defaults to llama-3.3-70b-versatile

Cost: Groq free tier covers ~200-500K tokens/day at 1000-1200 tok/email.
Fast — usually ~250-400ms per email. Uses OpenAI-compatible API so we
reuse the openai SDK.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

log = logging.getLogger(__name__)

_DEFAULT_MODEL = 'llama-3.3-70b-versatile'
_MAX_BODY_CHARS = 6000

# Same schema as Anthropic version so the caller doesn't care which model ran.
_SYSTEM_PROMPT = """You are a strict email-to-CRM extractor for Procam Group,
an Indian project cargo / heavy transport / freight forwarding / warehousing
/ installation company.

The email may be a FORWARDED message from a Procam employee. Your job is to
identify the ORIGINAL EXTERNAL SENDER and their requirement — NOT the
employee who forwarded it. Any procamgroup.in / procamlogistics.com email
address is INTERNAL and must NEVER be returned as the customer email.

Output STRICT JSON with exactly these keys (all present, use null when
unknown). Do not add commentary, do not wrap in code fences.

{
  "is_business_lead": true|false,
  "lead_type": "inbound_rfq|prospect_inquiry|vendor_pitch|existing_customer_ops|banking_or_admin|newsletter_or_marketing|personal_or_other",
  "reject_reason": null | "short reason",
  "company": null | "company name",
  "contact_name": null | "person name",
  "designation": null | "title",
  "phone_primary": null | "+91-9876543210",
  "phone_secondary": null | "string",
  "email_primary": null | "external customer email — NOT procamgroup.in",
  "email_secondary": null | "string",
  "origin": null | "city, state",
  "destination": null | "city, state",
  "cargo_type": null | "what is being transported",
  "cargo_weight_mt": null | number,
  "cargo_dimensions": null | "L x W x H",
  "cargo_qty": null | "trucks / containers / pieces",
  "procam_vertical": "Heavy Cargo|Project Freight|Freight Forwarding|Warehousing|Installation|General Transport|Other",
  "requirement_type": "RFQ|Enquiry|Booking|Follow-up|Existing Customer|Other",
  "urgency": "High|Medium|Low",
  "target_date": null | "YYYY-MM-DD",
  "special_requirements": ["array of short strings"],
  "one_line_summary": "one sentence, max 140 chars",
  "next_action_suggested": "specific next step for sales rep",
  "confidence": 0.0-1.0,
  "extraction_notes": null | "one line if anything was ambiguous"
}

RULES:
- NEVER return a procamgroup.in / procamlogistics.com address as email_primary.
- If you cannot identify an external sender, return email_primary=null and
  set extraction_notes="original sender not identifiable".
- If required commercial fields are missing, set confidence <= 0.5.
- Prefer information found in headers, signature blocks, or forwarded body
  text over guesses.
- one_line_summary must describe the CUSTOMER's request, not the forwarder's.
"""


def is_enabled() -> bool:
    return bool(os.environ.get('GROQ_API_KEY'))


def extract(msg: dict, regex_result: dict) -> Optional[dict]:
    if not is_enabled():
        return None
    try:
        from openai import OpenAI                                    # noqa
    except ImportError:                                              # pragma: no cover
        log.warning('openai package not installed — Groq extractor disabled')
        return None

    # Prefer the parser's already-unwrapped text: for a forwarded lead that
    # is the ORIGINAL customer message with the employee's covering note and
    # the forward headers stripped, which is exactly what we want the model
    # to reason over.
    body = (regex_result or {}).get('body_text') or \
        ((msg or {}).get('body') or {}).get('content') or ''
    subject = (regex_result or {}).get('subject') or (msg or {}).get('subject') or ''
    from_addr = (((msg or {}).get('from') or {}).get('emailAddress') or {}).get('address') or ''
    # Strip HTML crudely if the body is html
    if '<' in body and '>' in body:
        body = re.sub(r'<[^>]+>', ' ', body)
        body = re.sub(r'\s+', ' ', body)
    body = body.strip()[:_MAX_BODY_CHARS]

    user = (f'SUBJECT: {subject}\n'
            f'IMMEDIATE FROM (may be internal employee if forwarded): {from_addr}\n'
            f'BODY:\n{body}\n')

    client = OpenAI(api_key=os.environ['GROQ_API_KEY'],
                    base_url='https://api.groq.com/openai/v1')
    model = os.environ.get('EMAIL_AI_MODEL_GROQ', _DEFAULT_MODEL)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {'role': 'system', 'content': _SYSTEM_PROMPT},
                {'role': 'user',   'content': user},
            ],
            response_format={'type': 'json_object'},
            temperature=0.1,
            max_tokens=1200,
        )
    except Exception as e:
        log.warning('Groq extractor call failed: %s', e)
        return None

    raw = (resp.choices[0].message.content or '').strip()
    try:
        data = json.loads(raw)
    except Exception:
        # Try to recover a JSON object from the response
        m = re.search(r'\{.*\}', raw, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except Exception:
            return None

    # Guard: never pass an internal Procam address as the customer email.
    ep = (data.get('email_primary') or '').lower()
    if ep.endswith('@procamgroup.in') or ep.endswith('@procamlogistics.com'):
        data['email_primary'] = None
        data['needs_review'] = True
        data['extraction_notes'] = (
            (data.get('extraction_notes') or '') +
            ' | Suppressed internal Procam address as customer email.'
        ).strip(' |')

    # Ensure confidence exists
    if 'confidence' not in data or data['confidence'] is None:
        # Heuristic: high if company + email + requirement present, else medium
        has_ident = bool(data.get('company') and data.get('email_primary'))
        has_req   = bool(data.get('cargo_type') or data.get('origin') or data.get('destination'))
        data['confidence'] = 0.85 if (has_ident and has_req) else 0.5

    return data
