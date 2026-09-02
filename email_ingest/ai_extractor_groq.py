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
import time
from typing import Optional

log = logging.getLogger(__name__)

# Groq retires models on a rolling basis, and a retired id returns 404
# model_not_found — which would silently drop every email back to
# regex-only extraction. So we keep a candidate list and remember the first
# one that actually answers. EMAIL_AI_MODEL_GROQ still wins when set, but
# the others remain as fallback so a retirement degrades rather than breaks.
# Ordered best-first for structured extraction. Verified against a live
# Groq key on 2026-09-02; the Llama entries are kept last for accounts or
# regions that still serve them.
_MODEL_CANDIDATES = (
    'openai/gpt-oss-120b',
    'openai/gpt-oss-20b',
    'qwen/qwen3.8-27b',
    'qwen/qwen3.6-27b',
    'llama-3.3-70b-versatile',
    'meta-llama/llama-4-scout-17b-16e-instruct',
    'llama-3.1-8b-instant',
)
_RESOLVED_MODEL = None          # first candidate that answered, cached
_MAX_BODY_CHARS = 6000

# Groq's free tier is metered on tokens-per-minute (8k TPM at time of
# writing) and one email costs ~4-5k, so bursts and backfills hit 429
# routinely. A 429 is transient — retry it rather than dropping the email
# to regex-only extraction. Groq tells us how long to wait; we honour that,
# capped so a webhook request never hangs.
_RATE_LIMIT_RETRIES = 4
_RATE_LIMIT_MAX_SLEEP = 12.0

# Circuit breaker. Once the daily token cap is hit, every further call in
# this process is guaranteed to fail, so stop making them — a backfill of
# a few hundred messages would otherwise spend a doomed round trip each.
# Re-armed after a cool-off so a long-running process picks the quota back
# up when it resets.
_EXHAUSTED_UNTIL = 0.0
_EXHAUSTED_COOLOFF = 900.0      # 15 minutes

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
    if not os.environ.get('GROQ_API_KEY'):
        return False
    if _EXHAUSTED_UNTIL and time.time() < _EXHAUSTED_UNTIL:
        return False            # quota spent; don't waste the round trip
    return True


def _client():
    from openai import OpenAI                                    # noqa
    return OpenAI(api_key=os.environ['GROQ_API_KEY'],
                  base_url='https://api.groq.com/openai/v1')


def list_models() -> list:
    """Model ids this API key can actually use. Diagnostic helper —
    `python -c "from email_ingest import ai_extractor_groq as g;
    print(g.list_models())"`."""
    if not is_enabled():
        return []
    try:
        return sorted(m.id for m in _client().models.list().data)
    except Exception as e:                                       # noqa: BLE001
        log.warning('Groq model listing failed: %s', e)
        return []


def _candidate_models() -> tuple:
    """Models to try, best first. An explicitly configured model leads."""
    if _RESOLVED_MODEL:
        return (_RESOLVED_MODEL,)
    explicit = (os.environ.get('EMAIL_AI_MODEL_GROQ') or '').strip()
    if explicit:
        return (explicit,) + tuple(m for m in _MODEL_CANDIDATES
                                   if m != explicit)
    return _MODEL_CANDIDATES


def _is_model_missing(err) -> bool:
    text = str(err).lower()
    return ('model_not_found' in text or 'does not exist' in text
            or 'decommissioned' in text or 'has been deprecated' in text)


def _is_rate_limited(err) -> bool:
    text = str(err).lower()
    return 'rate_limit' in text or 'error code: 429' in text or '429' == text[:3]


def _is_daily_limit(err) -> bool:
    """Distinguish a per-day quota from a per-minute burst limit."""
    text = str(err).lower()
    return 'tokens per day' in text or '(tpd)' in text or 'requests per day' in text


def _retry_after_seconds(err, attempt: int) -> float:
    """How long to wait before retrying a 429. Groq embeds the answer in the
    message ("Please try again in 360ms" / "in 2.5s"); fall back to
    exponential backoff when it doesn't."""
    m = re.search(r'try again in\s*([0-9.]+)\s*(ms|s)\b', str(err), re.I)
    if m:
        secs = float(m.group(1))
        if m.group(2).lower() == 'ms':
            secs /= 1000.0
        # A hair of headroom — the window is measured server-side.
        secs += 0.25
    else:
        secs = 2.0 ** attempt
    return min(secs, _RATE_LIMIT_MAX_SLEEP)


def _is_json_mode_unsupported(err) -> bool:
    """Some models reject response_format=json_object. We can still use
    them — the caller recovers the JSON object from the raw text."""
    text = str(err).lower()
    return 'response_format' in text or 'json_object' in text


def _chat(client, model: str, user: str, json_mode: bool = True):
    kw = dict(
        model=model,
        messages=[{'role': 'system', 'content': _SYSTEM_PROMPT},
                  {'role': 'user',   'content': user}],
        temperature=0.1,
        # Generous: the reasoning-style models spend tokens before the JSON,
        # and a truncated object is worse than a slightly costlier call.
        max_tokens=2500,
    )
    if json_mode:
        kw['response_format'] = {'type': 'json_object'}
    return client.chat.completions.create(**kw)


def extract(msg: dict, regex_result: dict) -> Optional[dict]:
    if not is_enabled():
        return None
    try:
        import openai                                                # noqa
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

    global _RESOLVED_MODEL
    client = _client()
    resp = None
    tried = []
    for model in _candidate_models():
        tried.append(model)
        try:
            resp = None
            for attempt in range(_RATE_LIMIT_RETRIES + 1):
                try:
                    resp = _chat(client, model, user, json_mode=True)
                    break
                except Exception as je:                          # noqa: BLE001
                    if _is_json_mode_unsupported(je):
                        log.info('Groq model %s rejects JSON mode — retrying '
                                 'without it (JSON is recovered from the raw '
                                 'reply)', model)
                        resp = _chat(client, model, user, json_mode=False)
                        break
                    if _is_rate_limited(je) and attempt < _RATE_LIMIT_RETRIES:
                        # A per-minute (TPM) limit clears in seconds and is
                        # worth waiting for. A per-day (TPD) limit is not:
                        # Groq asks for minutes, so retrying just burns time
                        # on every message. Fail fast and let the caller fall
                        # back to regex-only extraction.
                        if _is_daily_limit(je):
                            global _EXHAUSTED_UNTIL
                            _EXHAUSTED_UNTIL = time.time() + _EXHAUSTED_COOLOFF
                            log.warning('Groq DAILY token limit reached — '
                                        'pausing AI extraction for %.0f min. '
                                        'Leads still ingest, without '
                                        'AI-extracted fields.',
                                        _EXHAUSTED_COOLOFF / 60)
                            return None
                        wait = _retry_after_seconds(je, attempt)
                        if wait >= _RATE_LIMIT_MAX_SLEEP:
                            log.warning('Groq asked to wait %.0fs — longer '
                                        'than we will hold a request. '
                                        'Skipping AI for this message.', wait)
                            return None
                        log.info('Groq rate limited on %s — retrying in %.2fs '
                                 '(attempt %d/%d)', model, wait, attempt + 1,
                                 _RATE_LIMIT_RETRIES)
                        time.sleep(wait)
                        continue
                    raise
            if resp is None:
                raise RuntimeError(
                    f'Groq rate limit not cleared after '
                    f'{_RATE_LIMIT_RETRIES} retries on {model}')
            if model != _RESOLVED_MODEL:
                log.info('Groq extractor using model %s', model)
            _RESOLVED_MODEL = model
            break
        except Exception as e:                                   # noqa: BLE001
            if _is_model_missing(e):
                # Retired or not licensed to this key — try the next one.
                log.warning('Groq model %s unavailable, trying next: %s',
                            model, str(e)[:140])
                if _RESOLVED_MODEL == model:
                    _RESOLVED_MODEL = None      # re-resolve on next call
                continue
            log.warning('Groq extractor call failed on %s: %s', model, e)
            return None
    if resp is None:
        log.error('Groq extractor: none of the candidate models were '
                  'available (tried %s). Run '
                  'email_ingest.ai_extractor_groq.list_models() to see what '
                  'this key can use, then set EMAIL_AI_MODEL_GROQ.',
                  ', '.join(tried))
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
