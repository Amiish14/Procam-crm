"""
v2026-08 — Employee-forwarder classification hint parser.

When a Procam employee forwards an email to leads@procamgroup.in they may
write a single word (or short phrase) at the top of the body to hint the
intended CRM classification. We recognise a configurable vocabulary of
those hints and pass the result through the ingest pipeline so:

    * Sales pipeline routing can honour the employee's judgement
    * The Lead detail card shows a clear "Classified as: Opportunity" chip
    * The AI extractor uses the hint as extra signal rather than guessing

The classifier is deliberately lightweight — a case-insensitive prefix
scan over the first ~8 lines of the body. If nothing matches, returns
None and the AI extractor falls back to its own judgement.
"""
from __future__ import annotations

import re
from typing import Optional

# Canonical vocabulary. Left-hand key is the normalised label written into
# the Lead's email_extracted_json.classification field. The right-hand list
# is the set of synonym words the classifier recognises when the employee
# types them (case-insensitive, exact word at start of a line).
CLASSIFICATIONS = {
    'Lead':            ['lead', 'new lead', 'fresh lead'],
    'Opportunity':     ['opportunity', 'opp', 'opportunity created', 'raise opportunity', 'raise opp'],
    'RFQ':             ['rfq', 'quotation request', 'request for quote', 'request for quotation',
                        'quote request'],
    'Follow-up':       ['follow-up', 'followup', 'follow up', 'chase'],
    'Customer Query':  ['customer query', 'query', 'question', 'enquiry', 'inquiry'],
    'Booking':         ['booking', 'confirmed', 'book', 'confirm shipment'],
    'Complaint':       ['complaint', 'escalation', 'issue', 'grievance'],
    'Information':     ['fyi', 'for your information', 'info', 'information',
                        'for reference', 'shared for reference'],
    'Not Interested':  ['not interested', 'declined', 'reject', 'rejected'],
    'Existing Customer': ['existing customer', 'existing client', 'repeat'],
    'Other':           ['other', 'general'],
}

# Word-boundary flatten for fast lookup
_LOOKUP = {}
for canonical, synonyms in CLASSIFICATIONS.items():
    for s in synonyms:
        _LOOKUP[s.lower()] = canonical

# Regex matches: optional prefix (bullet, dash, colon, greater-than), then
# a candidate hint word, then either newline / colon / dash / period.
_HINT_LINE_RE = re.compile(
    r'^\s*(?:[-*>]+\s*)?(?:hi\s+team\s*[,:.]?\s*)?'
    r'(?:please\s+(?:log|treat|classify|create)\s+(?:as|this\s+as)\s+)?'
    r'([A-Za-z][A-Za-z\-\s]{1,40})'
    r'\s*(?:[:\-—.,]|$)',
    re.IGNORECASE,
)


def extract_classification(body_text: str,
                           subject: str = None) -> Optional[dict]:
    """Scan the first few lines of a forwarded email for an employee hint.

    Returns a dict {label, source_line, employee_note} or None if nothing
    recognisable is found.

    Prefers, in order:
      1. First line of the body if it matches a known hint (most common)
      2. Subject line if prefixed with 'Fwd: Lead —' or similar
      3. Any of the first 8 lines
    """
    if not body_text:
        return None

    lines = [l.strip() for l in body_text.splitlines() if l.strip()]

    def _try(line: str) -> Optional[dict]:
        # Direct match on the whole line ignoring trailing punctuation
        stripped = re.sub(r'[:\-—.,!]+$', '', line).strip().lower()
        if stripped in _LOOKUP:
            return {'label': _LOOKUP[stripped],
                    'source_line': line,
                    'employee_note': None}
        # Regex extraction of leading token
        m = _HINT_LINE_RE.match(line)
        if not m:
            return None
        token = re.sub(r'\s+', ' ', m.group(1).strip().lower())
        # progressively shrink token to catch multi-word hints
        for tokens in (token, token.split(' ', 1)[0]):
            if tokens in _LOOKUP:
                # Remainder of the line becomes the employee note
                note_raw = line[m.end():].strip(' :-—.,')
                return {
                    'label': _LOOKUP[tokens],
                    'source_line': line,
                    'employee_note': note_raw or None,
                }
        return None

    # 1. First non-empty body line — most common forwarder pattern.
    if lines:
        r = _try(lines[0])
        if r:
            return r

    # 2. Subject line variants like "Fwd: Lead — Adani Power"
    if subject:
        subj = re.sub(r'^(?:fwd?|fw|re)\s*:\s*', '', subject.strip(), flags=re.I)
        r = _try(subj)
        if r:
            return r

    # 3. First 8 lines as a last resort
    for line in lines[:8]:
        r = _try(line)
        if r:
            return r

    return None
