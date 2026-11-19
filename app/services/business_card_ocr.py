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

PART 2 (15 Nov) added, around that core and without replacing it:

    card_image      the picture is straightened, cropped, evened out
                    and scaled before anybody reads it, and a PDF is
                    turned into pictures
    card_read       a second pass on the server, behind a switch,
                    which also decides which way up the card is
    card_fields     phones in E.164, the name split, the address
                    split into city / state / country / postal code
    card_confidence a score and a REASON per field, so the reviewer
                    knows which three of sixteen fields to look at

Front and back are one card: `sides=` takes the second picture and
everything read off it joins the first, because the address and the
second number are usually printed on the back.
"""
from __future__ import annotations

import base64
import json
import os
import re

from app.services import card_confidence


#: What an image block may carry. A PDF is NOT one of these, which
#: is the fault of 15 Nov: the PDF specimen was sent with
#: `media_type: application/pdf` inside an image block, refused, and
#: the refusal was swallowed.
IMAGE_TYPES = ('image/png', 'image/jpeg', 'image/gif', 'image/webp')

#: How many pictures of one card are sent. Front, back, and no more:
#: the rest of a PDF is somebody's brochure.
MAX_PICTURES = 2

#: Per picture, before base64. The preparation already scales to
#: 1600px, so this only ever catches a picture that skipped it.
MAX_PICTURE_BYTES = 4 * 1024 * 1024


def _log():
    """The CRM's logger where there is an app, the module's otherwise.

    The scanner is called from a request, from a script and from a
    test. All three have to be able to say "the reader failed", and
    `current_app` outside a request context raises.
    """
    import logging

    try:
        from flask import current_app
        if current_app:
            return current_app.logger
    except Exception:                                     # noqa: BLE001
        pass
    return logging.getLogger('business_card')


def service_model():
    """The model the image service is asked for, and where it came from."""
    return os.environ.get('BUSINESS_CARD_MODEL') or _MODEL


def service_ready():
    """(ready, why not) — without ever reading the key itself."""
    if not os.environ.get('ANTHROPIC_API_KEY'):
        return False, ('no key is visible to this process '
                       '(ANTHROPIC_API_KEY)')
    try:
        import anthropic                                  # noqa: F401
    except Exception as exc:                              # noqa: BLE001
        return False, f'the client library is unavailable ({exc})'
    return True, ''


def _picture_blocks(pictures):
    """The pictures the service will accept, as content blocks."""
    blocks, notes = [], []
    for data, mime in (pictures or [])[:MAX_PICTURES]:
        kind = (mime or '').split(';')[0].strip().lower()
        if kind not in IMAGE_TYPES:
            notes.append(f'not sent to the reader: {kind or "unknown type"} '
                         f'is not a picture it accepts')
            continue
        if len(data) > MAX_PICTURE_BYTES:
            notes.append(f'not sent to the reader: {len(data) // 1024} KB '
                         f'is over the limit')
            continue
        blocks.append({'type': 'image',
                       'source': {'type': 'base64', 'media_type': kind,
                                  'data': base64.b64encode(data).decode()}})
    return blocks, notes


def read_with_service(pictures):
    """Ask the image service to read these pictures of one card.

    Returns {'fields', 'raw', 'error', 'notes', 'ms', 'status'} and
    NEVER raises. Every way this can fail now has a reason in words
    and a WARNING in the log: on 15 Nov it failed on live for every
    card and journalctl had nothing in it at all.

    The pictures are the PREPARED ones. Sending the original was the
    other half of that fault — the straightening, cropping and
    contrast were computed and then thrown away, and a PDF card was
    sent as a PDF inside an image block.
    """
    import time

    out = {'fields': None, 'raw': '', 'error': '', 'notes': [],
           'ms': 0, 'status': '', 'configured': True}
    ready, why = service_ready()
    if not ready:
        # NOT configured is not FAILED. Running on the on-server
        # reader alone is a choice (15 Nov), and a chosen
        # configuration must not report itself as a fault on every
        # card — which is what made the report call a good reading a
        # failure and the scan page show a banner over a form that
        # was correctly filled.
        out['configured'] = False
        out['error'] = ''
        out['notes'].append(why)
        return out

    blocks, notes = _picture_blocks(pictures)
    out['notes'] = notes
    if not blocks:
        out['error'] = ('nothing could be sent to the reader'
                        + (f' — {notes[0]}' if notes else ''))
        return out

    import anthropic

    started = time.time()
    try:
        client = anthropic.Anthropic(
            api_key=os.environ['ANTHROPIC_API_KEY'])
        message = client.messages.create(
            model=service_model(), max_tokens=800,
            messages=[{'role': 'user',
                       'content': blocks + [{'type': 'text',
                                             'text': _PROMPT}]}])
    except Exception as exc:                              # noqa: BLE001
        out['ms'] = int((time.time() - started) * 1000)
        out['status'] = str(getattr(exc, 'status_code', '') or '')
        out['error'] = _why(exc)
        return out
    out['ms'] = int((time.time() - started) * 1000)
    out['status'] = '200'
    raw = ''.join(b.text for b in message.content
                  if hasattr(b, 'text')).strip()
    out['raw'] = raw
    found = re.search(r'\{[\s\S]*\}', raw)
    if not found:
        out['error'] = 'the reader answered without any JSON in it'
        return out
    try:
        parsed = json.loads(found.group(0))
    except Exception as exc:                              # noqa: BLE001
        out['error'] = f'the reader\'s answer is not readable JSON ({exc})'
        return out
    if not isinstance(parsed, dict):
        # A model that answers with a list or a string must not be
        # allowed to bypass validation.
        out['error'] = 'the reader answered with something other than an '\
                       'object'
        return out
    from app.services import card_parse as _cp
    out['fields'] = _cp.normalise(parsed)
    return out


def _nothing_read(notes):
    """Why there is no reading at all, in one line a person can act on."""
    from app.services import card_read

    _reader, why = card_read.text_reader()
    if why:
        return (f'nothing read this card: no image service is '
                f'configured and {why}')
    return ('nothing read this card: the on-server reader found no '
            'text on it')


def _why(exc):
    """An exception from the client as something a person can act on."""
    status = getattr(exc, 'status_code', None)
    name = type(exc).__name__
    text = str(exc)[:300]
    if status:
        return f'HTTP {status} from the image service ({name}): {text}'
    low = text.lower()
    if 'connect' in low or 'timeout' in low or 'ssl' in low \
            or 'name resolution' in low or 'proxy' in low:
        return (f'the server could not reach the image service '
                f'({name}): {text}')
    return f'{name}: {text}'


def _on_the_card(value, raw_text):
    """Whether this value is actually printed on the card.

    A company read off the email domain is a suggestion; one printed
    across the top is a fact. The review screen marks the first.
    """
    flat = re.sub(r'[^a-z0-9]', '', str(value or '').lower())
    body = re.sub(r'[^a-z0-9]', '', str(raw_text or '').lower())
    if not flat:
        return True
    if not body:
        return True        # nothing to check against: claim nothing
    return flat in body


def _as_fields(parsed):
    """One reading in the shape the confidence score compares."""
    from app.services import card_fields, card_parse

    if not parsed:
        return {}
    try:
        flat = card_parse.to_contact_fields(parsed)
        flat['raw_text'] = parsed.get('raw_text') or ''
        return card_fields.enrich(flat, raw_text=flat['raw_text'])
    except Exception:                                     # noqa: BLE001
        return dict(parsed)


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
                          card_text: str = None,
                          *, sides=None, preprocess: bool = True) -> dict:
    """Extract a card. Vision where available, deterministic always.

    `card_text` is any text already read off the card — pasted by the
    user, read by the on-server pass, or produced by the PDF when it
    holds text and no picture. When the image service is unavailable
    this is the whole extraction; when both are present the two are
    cross-checked and the disagreements are named.

    `sides` is [(bytes, mime)] for the back of the card and any
    further pictures of it. One card, one review form.

    Never raises — always returns ``{'extracted', 'raw', 'error',
    'confidence', 'notes'}``.
    """
    from app.services import card_fields, card_image, card_parse, card_read

    notes, pages, evidence = [], [], []
    pictures = [(image_bytes, mime)] + list(sides or [])
    if preprocess and image_bytes:
        try:
            ready = card_image.prepare(image_bytes, mime,
                                       sides=list(sides or []))
            notes.extend(ready.notes)
            pages = list(ready.pages)
            if ready.text and not card_text:
                card_text = ready.text
        except Exception as exc:                          # noqa: BLE001
            notes.append(f'the picture could not be prepared ({exc})')
    if pages:
        pictures = [(png, 'image/png') for png in pages]

    # The second pass, when the box has it and the switch is on. It
    # also decides which way up each picture is.
    if pages and not card_text:
        read_text, read_notes, evidence = \
            card_read.read_pages_with_evidence(pages)
        notes.extend(read_notes)
        if read_text.strip():
            card_text = read_text

    fallback = card_parse.parse(card_text) if card_text else None

    def finish(model_out, raw, error):
        readings = [r for r in (model_out, fallback) if r]
        if model_out and fallback:
            merged = card_parse.cross_check(model_out, fallback)
        elif model_out:
            merged = model_out
        elif fallback:
            merged = fallback
            error = error or None
        else:
            return {'extracted': {}, 'raw': raw, 'error': error,
                    'confidence': {}, 'notes': notes}
        out = card_parse.to_contact_fields(merged)
        out['conflicts'] = merged.get('conflicts') or []
        out['raw_text'] = merged.get('raw_text') or (card_text or '')
        # Where a field was WORKED OUT rather than read, the review
        # screen has to say so — it is the difference between a fact
        # and a good guess, and the guesses are what get corrected.
        derived = set()
        if out.get('company') and not _on_the_card(out['company'],
                                                   out['raw_text']):
            derived.add('company')
        before = dict(out)
        out = card_fields.enrich(out, raw_text=out['raw_text'])
        for key in ('city', 'state', 'country', 'postal_code'):
            # Split out of the address block is still READ off the
            # card. Marked only when the value is not printed anywhere
            # on it — a state implied by a city, a country implied by
            # a PIN code. Those are the ones worth a glance.
            if out.get(key) and not before.get(key) \
                    and not _on_the_card(out[key], out['raw_text']):
                derived.add(key)
        out['confidence'] = card_confidence.score(
            out, readings=[_as_fields(r) for r in readings],
            conflicts=out['conflicts'], derived=derived, notes=notes)
        # CRM-SCAN-02: content-first candidates for company, name,
        # designation and state, scored from every reading the reader
        # produced, the email domain, the CRM's own accounts and what
        # this domain was corrected to before. The runners-up go to the
        # review screen as a dropdown; the evidence to the accuracy page.
        try:
            from app.services import card_smart
            out = card_smart.rescore(
                out, out['raw_text'], *[r.get('text') or '' for r in evidence])
        except Exception as exc:                          # noqa: BLE001
            notes.append(f'the candidate scorer could not run ({exc})')
        out['ocr_evidence'] = [
            {k: r.get(k) for k in ('side', 'rotation', 'how', 'score', 'text')}
            for r in evidence][:24]
        out['confidence_summary'] = card_confidence.summary(
            out['confidence'])
        out['notes'] = notes
        return {'extracted': out, 'raw': raw, 'error': error,
                'confidence': out['confidence'], 'notes': notes}

    read = read_with_service(pictures)
    notes.extend(read['notes'])
    if read['fields']:
        return finish(read['fields'], read['raw'], None)

    if read['configured']:
        # NEVER SWALLOWED (live, 15 Nov): every specimen came back
        # empty and journalctl had not one line about it, because this
        # path returned the reason as a string and nobody printed it.
        why = read['error'] or ('the image service answered, but not '
                                'with card fields')
        _log().warning('business card: the image service failed — %s', why)
        return finish(None, read['raw'], why)

    # No image service configured. Then whatever the on-server reader
    # or the typed text produced IS the reading, and a card read that
    # way is not an error — it is the configuration working.
    if fallback:
        return finish(None, '', None)
    return finish(None, '', _nothing_read(notes))


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
            # Compared on digits: "+91 90000-20001" and "09000020001" are
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
