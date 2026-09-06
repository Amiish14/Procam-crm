"""Business card OCR via Claude Vision (Phase 12).

Two entry points:

    extract_business_card(image_bytes, mime='image/jpeg')
        → {'extracted': dict, 'raw': str, 'error': str | None}

    find_duplicates(extracted: dict, db)
        → {'accounts': [...], 'contacts': [...]}

Both are safe to call without an API key — the extractor returns an
`error` string and the endpoint falls back to a human review with an
empty extraction rather than failing the upload.
"""
from __future__ import annotations

import base64
import json
import os
import re


_MODEL = os.environ.get('BUSINESS_CARD_MODEL', 'claude-haiku-4-5-20251001')

_PROMPT = '''You are extracting structured contact information from a business card image.
Return a strict JSON object with these keys (use empty string if not present, never null):
{
  "name": "...",
  "designation": "...",
  "department": "...",
  "company": "...",
  "email": "...",
  "mobile": "...",
  "telephone": "...",
  "website": "...",
  "address": "...",
  "city": "...",
  "state": "...",
  "country": "...",
  "linkedin_url": ""
}

Do not add any commentary. Return only the JSON object. If a field is not visible or unclear, use empty string.'''


def extract_business_card(image_bytes: bytes,
                          mime: str = 'image/jpeg') -> dict:
    """Send an image to Claude Vision and parse a JSON response.

    Never raises — always returns a dict of the shape
    ``{'extracted': dict, 'raw': str, 'error': str | None}``.
    """
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return {'extracted': {}, 'raw': '',
                'error': 'ANTHROPIC_API_KEY not configured'}
    try:
        import anthropic
    except Exception as exc:                                        # pragma: no cover
        return {'extracted': {}, 'raw': '',
                'error': f'anthropic package unavailable: {exc}'}

    try:
        client = anthropic.Anthropic(api_key=api_key)
        b64 = base64.b64encode(image_bytes).decode()
        msg = client.messages.create(
            model=_MODEL,
            max_tokens=800,
            messages=[{
                'role': 'user',
                'content': [
                    {'type': 'image',
                     'source': {'type': 'base64',
                                'media_type': mime,
                                'data': b64}},
                    {'type': 'text', 'text': _PROMPT},
                ],
            }],
        )
        raw = ''.join([b.text for b in msg.content
                       if hasattr(b, 'text')]).strip()
        m = re.search(r'\{[\s\S]*\}', raw)
        parsed = json.loads(m.group(0)) if m else {}
        # Coerce values to strings — the prompt asks for empty strings not
        # nulls, but defensively coerce anyway.
        parsed = {k: ('' if v is None else str(v)) for k, v in parsed.items()}
        return {'extracted': parsed, 'raw': raw, 'error': None}
    except Exception as exc:
        return {'extracted': {}, 'raw': '', 'error': str(exc)}


def _domain_of(url_or_email: str) -> str:
    s = (url_or_email or '').strip().lower()
    if not s:
        return ''
    if '@' in s:
        return s.split('@', 1)[1].split('/')[0]
    s = s.replace('http://', '').replace('https://', '')
    return s.split('/')[0].strip()


def find_duplicates(extracted: dict, db) -> dict:
    """Fuzzy-match extraction against existing Companies + Contacts.

    Returns ``{'accounts': [...], 'contacts': [...]}`` where each entry is
    a dict of ``{id, name, score, reason}``.  Deduped by id within each
    category.  Safe when either table is empty.
    """
    from app import Company, Contact  # imported lazily to avoid boot cycles

    matches = {'accounts': [], 'contacts': []}
    if not extracted:
        return matches

    email        = (extracted.get('email') or '').strip().lower()
    phone        = (extracted.get('mobile') or '').strip()
    company_name = (extracted.get('company') or '').strip()
    person_name  = (extracted.get('name') or '').strip()
    website      = _domain_of(extracted.get('website') or '')

    try:
        if company_name:
            for c in Company.query.filter(
                    Company.name.ilike(f'%{company_name}%')).limit(5).all():
                matches['accounts'].append(
                    {'id': c.id, 'name': c.name, 'score': 90,
                     'reason': 'name match'})
        if website:
            for c in Company.query.filter(
                    Company.website.ilike(f'%{website}%')).limit(3).all():
                matches['accounts'].append(
                    {'id': c.id, 'name': c.name, 'score': 95,
                     'reason': 'website match'})
    except Exception:
        pass

    try:
        if email:
            for c in Contact.query.filter(
                    Contact.email.ilike(email)).limit(3).all():
                matches['contacts'].append(
                    {'id': c.id, 'name': c.name, 'score': 100,
                     'reason': 'email exact'})
        if phone:
            for c in Contact.query.filter(
                    Contact.mobile == phone).limit(3).all():
                matches['contacts'].append(
                    {'id': c.id, 'name': c.name, 'score': 100,
                     'reason': 'mobile exact'})
        if person_name:
            for c in Contact.query.filter(
                    Contact.name.ilike(f'%{person_name}%')).limit(3).all():
                matches['contacts'].append(
                    {'id': c.id, 'name': c.name, 'score': 70,
                     'reason': 'name match'})
    except Exception:
        pass

    for k in ('accounts', 'contacts'):
        seen = set()
        deduped = []
        for m in matches[k]:
            if m['id'] not in seen:
                deduped.append(m)
                seen.add(m['id'])
        matches[k] = deduped
    return matches
