"""
v2026-08 — AI extractor router.

Sits in front of the AI extractors and chooses the best available model:

    1. Groq / Llama         — primary (fast, cheap; needs GROQ_API_KEY)
    2. Anthropic / Claude   — fallback (used if Groq disabled OR its
                              confidence < THRESHOLD)

Also invokes the classifier module to capture the employee-forwarder's
hint (Lead / Opportunity / Follow-up / …) and injects it into the result.

Adds a `needs_review` flag when:
    * The AI couldn't pin down an external sender.
    * The AI's confidence is low.
    * Extraction fell back to regex-only.

The Lead detail UI shows a review banner when this flag is set.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from email_ingest import ai_extractor as anthropic_extractor
from email_ingest import ai_extractor_groq as groq_extractor
from email_ingest import classifier as _classifier

log = logging.getLogger(__name__)

_CONFIDENCE_FALLBACK = float(os.environ.get('EMAIL_AI_FALLBACK_THRESHOLD', '0.55'))


def extract(msg: dict, regex_result: dict) -> Optional[dict]:
    """Run the best available extractor. Returns the enriched dict or
    None if AI is entirely unavailable — in which case the caller keeps
    the regex-only result."""
    body = ((msg or {}).get('body') or {}).get('content') or ''
    subject = (msg or {}).get('subject') or ''

    # 1. Try Groq
    result = None
    used_model = None
    if groq_extractor.is_enabled():
        try:
            result = groq_extractor.extract(msg, regex_result)
            used_model = 'groq/llama-3.3-70b'
        except Exception as e:
            log.warning('Groq extractor exception: %s', e)
            result = None

    # 2. Fallback to Anthropic if Groq was absent or low-confidence
    low_conf = bool(result and (result.get('confidence') or 0) < _CONFIDENCE_FALLBACK)
    if (not result or low_conf) and anthropic_extractor.is_enabled():
        try:
            fallback = anthropic_extractor.extract(msg, regex_result)
            if fallback:
                # If we're upgrading from a low-confidence Groq result, keep the
                # more confident one but note the fallback happened.
                if result and low_conf:
                    log.info('Fallback to Anthropic (Groq conf=%s)', result.get('confidence'))
                    fallback['_upgraded_from_groq'] = result.get('confidence')
                result = fallback
                used_model = 'anthropic/claude'
        except Exception as e:
            log.warning('Anthropic fallback failed: %s', e)

    # 3. Attach classification from employee forwarder hint
    if result is not None:
        cls = _classifier.extract_classification(body, subject)
        if cls:
            result['classification']        = cls['label']
            result['classification_source'] = cls['source_line']
            if cls.get('employee_note'):
                result['employee_note']     = cls['employee_note']

        # 4. needs_review determination
        needs_review = bool(result.get('needs_review'))
        if not result.get('email_primary'):
            needs_review = True
        if (result.get('confidence') or 0) < 0.4:
            needs_review = True
        # If we STILL have a procam-internal address slip through, force review
        ep = (result.get('email_primary') or '').lower()
        if ep.endswith('@procamgroup.in') or ep.endswith('@procamlogistics.com'):
            result['email_primary'] = None
            needs_review = True
        result['needs_review'] = needs_review

        # 5. Metadata for the audit trail
        result['_ai_model'] = used_model
    return result


def is_enabled() -> bool:
    """Router is enabled if ANY underlying extractor is available."""
    return groq_extractor.is_enabled() or anthropic_extractor.is_enabled()
