"""Business card extraction — Claude Vision, checked by a deterministic parser.

Two entry points:

    extract_business_card(image_bytes, mime='image/jpeg', card_text=None)
        → {'extracted': dict, 'raw': str, 'error': str | None}

    find_duplicates(extracted: dict, db)
        → {'accounts': [...], 'contacts': [...]}

Vision was the only path, which made it a single point of failure: no
API key, no credit, or a malformed response and the reviewer got an empty
form. app/services/card_parse.py is now layered underneath it as

    a fallback     when the model returns nothing but text is available
    a cross-check  disagreements are named, not silently resolved
    a validation   the model's shape is forced into the contract

Both entry points remain safe to call without an API key.
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
                          mime: str = 'image/jpeg',
                          card_text: str = None) -> dict:
    """Extract a card. Vision where available, deterministic always.

    `card_text` is any text already read off the card — pasted by the
    user, or produced by an OCR step if one is ever added. When Vision
    is unavailable this is the whole extraction; when both are present
    the two are cross-checked.

    Never raises — always returns
    ``{'extracted': dict, 'raw': str, 'error': str | None}``.
    """
    from app.services import card_parse

    fallback = card_parse.parse(card_text) if card_text else None

    def finish(model_out, raw, error):
        if model_out and fallback:
            merged = card_parse.cross_check(model_out, fallback)
        elif model_out:
            merged = model_out
        elif fallback:
            merged = fallback
            error = error or None
        else:
            return {'extracted': {}, 'raw': raw, 'error': error}
        out = card_parse.to_contact_fields(merged)
        out['conflicts'] = merged.get('conflicts') or []
        out['raw_text'] = merged.get('raw_text') or (card_text or '')
        return {'extracted': out, 'raw': raw, 'error': error}

    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return finish(None, '', 'ANTHROPIC_API_KEY not configured')
    try:
        import anthropic
    except Exception as exc:                                        # pragma: no cover
        return finish(None, '', f'anthropic package unavailable: {exc}')

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
        if not isinstance(parsed, dict):
            # A model that answers with a list or a string must not be
            # allowed to bypass validation.
            return finish(None, raw, 'model returned a non-object')
        from app.services import card_parse as _cp
        return finish(_cp.normalise(parsed), raw, None)
    except Exception as exc:
        return finish(None, '', str(exc))


def _domain_of(url_or_email: str) -> str:
    s = (url_or_email or '').strip().lower()
    if not s:
        return ''
    if '@' in s:
        return s.split('@', 1)[1].split('/')[0]
    s = s.replace('http://', '').replace('https://', '')
    s = s.split('/')[0].strip()
    # A card printed "www.siemens.com" would not have matched a company
    # stored as "siemens.com", which defeats the point of matching on
    # domain at all (§39).
    if s.startswith('www.'):
        s = s[4:]
    return s


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

    from app.services.card_parse import normalise_phone

    # Every address and number on the card, not just the primary — a
    # duplicate found on the second number is still a duplicate, and
    # creating the contact anyway is how the same person ends up in the
    # CRM three times.
    # The union of the lists and the singular fields, never one or the
    # other: the review screen edits `email`/`mobile`, so preferring the
    # list would check the extraction the reviewer just corrected.
    def _all(list_key, *single_keys):
        vals = [v for v in (extracted.get(list_key) or []) if v]
        vals += [extracted.get(k) for k in single_keys if extracted.get(k)]
        seen, out = set(), []
        for v in vals:
            k = str(v).strip().lower()
            if k and k not in seen:
                seen.add(k)
                out.append(str(v).strip())
        return out

    emails = [e.lower() for e in _all('emails', 'email')]
    phones = _all('phones', 'mobile', 'telephone')
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
        for email in emails:
            for c in Contact.query.filter(
                    Contact.email.ilike(email)).limit(3).all():
                matches['contacts'].append(
                    {'id': c.id, 'name': c.name, 'score': 100,
                     'reason': f'email {email}'})
        if phones:
            # Compared on digits: "+91 98200-11223" and "09820011223" are
            # one number, and an exact string match would miss it.
            wanted = {normalise_phone(p) for p in phones}
            wanted.discard('')
            if wanted:
                for c in Contact.query.filter(
                        db.or_(Contact.mobile.isnot(None),
                               Contact.phone.isnot(None))).limit(4000).all():
                    for existing in (c.mobile, c.phone):
                        if existing and normalise_phone(existing) in wanted:
                            matches['contacts'].append(
                                {'id': c.id, 'name': c.name, 'score': 100,
                                 'reason': f'phone {existing}'})
                            break
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
