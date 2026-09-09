"""
Deterministic business-card parsing — text in, structured contact out.

Why this exists
    The scanner's only extraction path is Claude Vision, which returns
    structured JSON directly.  That means a single point of failure: no
    API key, no credit, a malformed response or a refusal, and the whole
    scan degrades to an empty review form.  It also means nothing ever
    checks the model's answer.

    This module is pure text processing — no API, no dependencies, no
    network.  It is used three ways, exactly as the brief asks:

        fallback     parse(text) when the model returned nothing
        cross-check  cross_check(model_output, text) flags disagreement
        validation   normalise() fixes shapes the model gets wrong
                     (a null where a string was promised, one phone in a
                     field the card had three of, a website that is
                     really an email domain)

Layout independence
    Nothing here assumes the name is above the company, or that the
    company is below the name.  Each field is found by what it *looks*
    like — an email is an email wherever it sits — and the two fields
    that genuinely need position (name, company) are scored against
    several independent signals and take the best candidate.

Output shape (every key always present):

    {'name', 'designation', 'company', 'emails', 'phones',
     'website', 'address', 'raw_text'}

`emails` and `phones` are lists, most significant first.  The CRM's
Contact model holds one of each, so the caller takes [0] and keeps the
rest in the review screen — but the parse never throws information away.
"""
from __future__ import annotations

import re


# ─── Vocabulary (extensible by design — see TITLES / SUFFIXES) ───────────

#: Job titles.  Matched as whole words, case-insensitively, so "Head"
#: matches "Head - Logistics" but not "Headway Shipping".
TITLES = (
    'managing director', 'general manager', 'senior manager',
    'deputy general manager', 'assistant general manager',
    'vice president', 'senior vice president', 'associate vice president',
    'chief executive officer', 'chief operating officer',
    'chief financial officer', 'chief technology officer',
    'business development manager', 'sales manager', 'branch manager',
    'regional manager', 'country manager', 'project manager',
    'operations manager', 'account manager', 'key account manager',
    'assistant manager', 'senior executive', 'senior engineer',
    'proprietor', 'president', 'chairman', 'director', 'manager',
    'partner', 'founder', 'co-founder', 'owner', 'principal',
    'head', 'engineer', 'executive', 'consultant', 'coordinator',
    'supervisor', 'officer', 'specialist', 'analyst', 'associate',
    'incharge', 'in-charge', 'lead', 'ceo', 'coo', 'cfo', 'cto', 'cmo',
    'md', 'gm', 'dgm', 'agm', 'avp', 'svp', 'vp', 'sr. manager',
    'asst. manager', 'mgr',
)

#: Legal / organisational suffixes and business words.  A line carrying
#: one of these is very likely the company.
SUFFIXES = (
    'private limited', 'pvt ltd', 'pvt. ltd', 'pvt limited', 'limited',
    'ltd', 'llp', 'llc', 'inc', 'incorporated', 'corporation', 'corp',
    'gmbh', 'ag', 'bv', 'nv', 'sa', 'srl', 'spa', 'plc', 'pte',
    'sdn bhd', 'co., ltd', 'company', 'and co', '& co', 'group',
    'holdings', 'industries', 'enterprises', 'ventures',
    'logistics', 'shipping', 'freight', 'forwarders', 'forwarding',
    'transport', 'transports', 'carriers', 'cargo', 'marine',
    'technologies', 'technology', 'solutions', 'systems', 'services',
    'engineering', 'engineers', 'infrastructure', 'infra', 'projects',
    'international', 'global', 'overseas', 'exports', 'imports',
    'trading', 'traders', 'agencies', 'agency', 'associates',
    'consultants', 'consultancy', 'steel', 'cement', 'power', 'energy',
    'chemicals', 'pharma', 'motors', 'automobiles', 'textiles',
)

#: Words that mark a line as part of a postal address.
ADDRESS_WORDS = (
    'road', 'rd', 'street', 'st', 'lane', 'marg', 'nagar', 'colony',
    'sector', 'block', 'plot', 'survey', 'floor', 'flr', 'tower',
    'building', 'bldg', 'complex', 'chambers', 'house', 'plaza',
    'estate', 'industrial area', 'phase', 'zone', 'park', 'avenue',
    'cross', 'main', 'opp', 'opposite', 'near', 'behind', 'above',
    'p.o', 'po box', 'pobox', 'gali', 'chowk', 'circle', 'highway',
    'bypass', 'district', 'taluka', 'village', 'post', 'pin', 'pincode',
    'no.', 'unit', 'suite', 'office no',
)

#: Labels a card puts in front of a number.  Order matters: the more
#: specific label must be tried first ("mobile" before "m").
PHONE_LABELS = (
    ('mobile', 'mobile'), ('mob', 'mobile'), ('cell', 'mobile'),
    ('handphone', 'mobile'), ('hp', 'mobile'),
    ('direct', 'direct'), ('dir', 'direct'),
    ('office', 'office'), ('off', 'office'), ('work', 'office'),
    ('landline', 'landline'), ('land', 'landline'),
    ('telephone', 'telephone'), ('tel', 'telephone'), ('phone', 'phone'),
    ('board', 'board'), ('reception', 'board'),
    ('fax', 'fax'), ('f', 'fax'),
    ('whatsapp', 'whatsapp'), ('wa', 'whatsapp'),
    ('t', 'telephone'), ('p', 'phone'), ('m', 'mobile'),
)

#: How trustworthy a number is as *the* number to call. Fax never wins.
_PHONE_RANK = {'mobile': 0, 'whatsapp': 1, 'direct': 2, 'phone': 3,
               'telephone': 4, 'office': 5, 'landline': 6, 'board': 7,
               '': 8, 'fax': 99}

_EMAIL_RE = re.compile(
    r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')

_URL_RE = re.compile(
    r'\b(?:https?://|www\.)[^\s,;<>"\']+'
    r'|\b[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.'
    r'(?:com|net|org|in|co\.in|co\.uk|io|biz|info|ae|sg|de|fr|jp|cn|'
    r'com\.au|us|ca|nl|it|es|se|ch|be|dk|no|fi|pl|ru|br|za|my|th|id|'
    r'vn|ph|kr|tw|hk|nz|ie|pt|gr|tr|il|sa|qa|kw|om|bh|eg|ng|ke)\b')

#: Deliberately loose — a card writes numbers every way there is.
#: Trimmed and length-checked afterwards rather than over-constrained here.
_PHONE_RE = re.compile(
    r'(?:\+\d{1,4}[\s.\-]?)?'
    r'(?:\(\d{1,5}\)[\s.\-]?)?'
    r'\d[\d\s.\-()]{6,18}\d')

_PIN_RE = re.compile(r'\b\d{5,6}\b')

#: Domains that say nothing about the employer.
_GENERIC_DOMAINS = frozenset({
    'gmail.com', 'googlemail.com', 'yahoo.com', 'yahoo.co.in',
    'yahoo.co.uk', 'hotmail.com', 'outlook.com', 'live.com', 'aol.com',
    'rediffmail.com', 'icloud.com', 'me.com', 'protonmail.com',
    'zoho.com', 'mail.com', 'ymail.com', 'msn.com', 'gmx.com',
})


# ─── small helpers ───────────────────────────────────────────────────────
def _clean(line: str) -> str:
    return re.sub(r'\s+', ' ', (line or '').strip(' \t\r\n|·•*-–—:')).strip()


def _has_word(line: str, words) -> bool:
    """Whole-word containment, case-insensitive.

    Substring matching would make "Head" fire on "Headway Shipping" and
    "st" on "Best Cargo", which is how naive parsers mislabel a company
    as an address.
    """
    low = ' ' + re.sub(r'[^a-z0-9&.\s\-]', ' ', line.lower()) + ' '
    low = re.sub(r'\s+', ' ', low)
    for w in words:
        if re.search(r'(?<![a-z0-9])' + re.escape(w) + r'(?![a-z0-9])', low):
            return True
    return False


def domain_of(value: str) -> str:
    """Bare registrable domain from a URL or an email address."""
    s = (value or '').strip().lower()
    if not s:
        return ''
    if '@' in s:
        s = s.rsplit('@', 1)[1]
    s = re.sub(r'^https?://', '', s)
    s = s.split('/')[0].split('?')[0].strip().strip('.')
    if s.startswith('www.'):
        s = s[4:]
    return s


def normalise_phone(value: str) -> str:
    """Digits (and a leading +) only — for comparing, never for display.

    "+91 98200-11223", "098200 11223" and "(0)9820011223" are one number
    to a human and three to a database, which is how duplicate contacts
    get created.
    """
    s = re.sub(r'[^\d+]', '', value or '')
    if s.startswith('00'):
        s = '+' + s[2:]
    plus = s.startswith('+')
    digits = s.lstrip('+')
    # Indian numbers are written with and without the country code and a
    # trunk 0; reduce to the last 10 digits so the forms agree.
    if not plus and len(digits) > 10 and digits.startswith('0'):
        digits = digits.lstrip('0')
    if len(digits) > 10 and digits.startswith('91') and len(digits) == 12:
        digits = digits[2:]
    return ('+' if plus and len(digits) > 10 else '') + digits


def _looks_like_person(line: str) -> bool:
    """Human-name shape: 2–4 capitalised words, no digits, no @."""
    if not line or '@' in line or any(ch.isdigit() for ch in line):
        return False
    if len(line) > 45:
        return False
    words = [w for w in re.split(r'\s+', line.strip()) if w]
    if not (1 < len(words) <= 5):
        return False
    for w in words:
        core = w.strip('.,()')
        if not core:
            continue
        if not core.replace('-', '').replace("'", '').isalpha():
            return False
    # ALL-CAPS cards are common, so caps are not evidence against a name.
    return True


# ─── field extraction ────────────────────────────────────────────────────
def extract_emails(text: str) -> list:
    out, seen = [], set()
    for m in _EMAIL_RE.finditer(text or ''):
        e = m.group(0).strip(' .,;:')
        low = e.lower()
        if low not in seen:
            seen.add(low)
            out.append(e)
    # A work address beats a personal one as the contact's primary.
    out.sort(key=lambda e: domain_of(e) in _GENERIC_DOMAINS)
    return out


def extract_phones(text: str) -> list:
    """Every number on the card, best-to-call first.

    Returns [{'value', 'label', 'normalised'}, ...] — the label is kept
    because "which of these three is the mobile" is exactly the question
    a reviewer would otherwise have to answer by hand.
    """
    found, seen = [], set()
    for raw_line in (text or '').splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Strip emails first, or their digits get read as phone numbers.
        line = _EMAIL_RE.sub(' ', line)
        low = line.lower()

        # Where each label appears. Cards commonly print two numbers on
        # one line — "Mobile 97012... | Board 040-661..." — so a single
        # label per line would tag the board number as a mobile.
        label_at = []
        for token, canonical in PHONE_LABELS:
            for m in re.finditer(
                    r'(?<![a-z])' + re.escape(token) + r'(?![a-z])', low):
                label_at.append((m.start(), m.end(), canonical, len(token)))
        # Longest token wins where two overlap ("mob" inside "mobile").
        label_at.sort(key=lambda t: (t[0], -t[3]))
        kept = []
        for item in label_at:
            if kept and item[0] < kept[-1][1]:
                continue
            kept.append(item)

        for m in _PHONE_RE.finditer(line):
            value = m.group(0).strip(' .,;:-')
            digits = re.sub(r'\D', '', value)
            # 7 digits filters out years, PIN codes and door numbers;
            # 15 is the E.164 maximum.
            if not (7 <= len(digits) <= 15):
                continue
            label = ''
            for start, end, canonical, _n in kept:
                if end <= m.start():
                    label = canonical      # nearest label to the left
                else:
                    break
            if not label:
                label = _label_from_shape(value)
            norm = normalise_phone(value)
            if norm in seen:
                continue
            seen.add(norm)
            found.append({'value': value, 'label': label,
                          'normalised': norm})
    found.sort(key=lambda p: _PHONE_RANK.get(p['label'], 8))
    return found


def _label_from_shape(value: str) -> str:
    """Unlabelled numbers still tell you what they are.

    An Indian mobile is ten digits starting 6-9; a landline carries an
    STD code and is written with a separator after it. Getting this right
    is what puts the right number in the Contact's mobile field.
    """
    digits = re.sub(r'\D', '', value)
    if digits.startswith('91') and len(digits) == 12:
        digits = digits[2:]
    digits = digits.lstrip('0')
    if len(digits) == 10 and digits[0] in '6789':
        return 'mobile'
    if value.strip().startswith('0') or re.match(r'^\d{2,5}[\s.\-]\d', value.strip()):
        return 'landline'
    return ''


def extract_website(text: str, emails=None) -> str:
    for m in _URL_RE.finditer(text or ''):
        cand = m.group(0).strip(' .,;:')
        if '@' in cand:
            continue
        d = domain_of(cand)
        if d and d not in _GENERIC_DOMAINS and '.' in d:
            return d
    # No website printed — the work email's domain is the company's site
    # far more often than not.
    for e in (emails or []):
        d = domain_of(e)
        if d and d not in _GENERIC_DOMAINS:
            return d
    return ''


def extract_designation(lines) -> str:
    best, best_score = '', 0
    for i, line in enumerate(lines):
        if '@' in line or not line:
            continue
        low = line.lower()
        for t in TITLES:
            if re.search(r'(?<![a-z])' + re.escape(t) + r'(?![a-z])', low):
                # A longer title is a more specific one: prefer
                # "General Manager" over the "Manager" inside it.
                score = len(t) * 10
                # A line that is *only* the title beats one that buries it.
                if len(line) < 45:
                    score += 30
                score -= i          # earlier lines are likelier
                if score > best_score:
                    best, best_score = line, score
                break
    return best


def _company_score(line: str, i: int, total: int, domain: str) -> int:
    if not line or '@' in line:
        return 0
    score = 0
    if _has_word(line, SUFFIXES):
        score += 100
    if domain:
        # The email domain is the strongest signal there is: "ambujacement"
        # in the address matches "Ambuja Cement Ltd" on the card.
        stem = re.sub(r'[^a-z0-9]', '', domain.split('.')[0])
        flat = re.sub(r'[^a-z0-9]', '', line.lower())
        if stem and len(stem) > 3 and (stem in flat or flat in stem):
            score += 120
    if line.isupper() and len(line) > 3:
        score += 15                       # printed prominently
    if _has_word(line, ADDRESS_WORDS):
        score -= 60                       # it is the address, not the firm
    if _looks_like_person(line):
        score -= 40                       # it is the person, not the firm
    if any(ch.isdigit() for ch in line):
        score -= 25
    if len(line) > 60:
        score -= 20
    # Company names sit near the top or bottom of a card, rarely mid-block.
    if total > 2 and (i <= 1 or i >= total - 2):
        score += 10
    return score


def extract_company(lines, domain: str = '') -> str:
    total = len(lines)
    scored = [(_company_score(l, i, total, domain), -i, l)
              for i, l in enumerate(lines)]
    scored = [s for s in scored if s[0] > 20]
    if not scored:
        return ''
    scored.sort(reverse=True)
    return scored[0][2]


def extract_name(lines, designation: str, company: str) -> str:
    """The person, found without assuming where the card printed them."""
    best, best_score = '', -999
    total = len(lines)
    for i, line in enumerate(lines):
        if not line or line in (designation, company):
            continue
        if '@' in line or _has_word(line, SUFFIXES) \
                or _has_word(line, ADDRESS_WORDS):
            continue
        if not _looks_like_person(line):
            continue
        score = 50
        score -= i * 3                    # names print high on the card
        if designation and designation in lines:
            # Immediately above or below the job title is the strongest
            # positional signal a card gives.
            d = lines.index(designation)
            if abs(i - d) == 1:
                score += 60
        if len(line.split()) in (2, 3):
            score += 20
        if re.match(r'^(mr|mrs|ms|dr|er|ca|prof)\.?\s', line.lower()):
            score += 25
        if i < total / 2:
            score += 10
        if score > best_score:
            best, best_score = line, score
    return best


def extract_address(lines, used) -> str:
    """Whatever is left that reads like a postal address."""
    out = []
    for line in lines:
        if not line or line in used:
            continue
        if '@' in line or _URL_RE.search(line):
            continue
        digits = re.sub(r'\D', '', line)
        # A line that is mostly a phone number is not the address.
        if len(digits) >= 7 and len(digits) >= len(line) * 0.4:
            continue
        if _has_word(line, ADDRESS_WORDS) or _PIN_RE.search(line) \
                or (',' in line and len(line) > 12):
            out.append(line)
    return ', '.join(out)


# ─── public API ──────────────────────────────────────────────────────────
def _company_from_domain(domain: str) -> str:
    stem = (domain or '').split('.')[0]
    stem = re.sub(r'[^A-Za-z0-9]+', ' ', stem).strip()
    if len(stem) < 3:
        return ''
    return stem.title()


def blank() -> dict:
    return {'name': '', 'designation': '', 'company': '', 'emails': [],
            'phones': [], 'website': '', 'address': '', 'raw_text': ''}


def parse(text: str) -> dict:
    """Free card text → the structured shape. Never raises."""
    result = blank()
    result['raw_text'] = text or ''
    if not (text or '').strip():
        return result

    lines = [_clean(l) for l in text.splitlines()]
    lines = [l for l in lines if l]

    emails = extract_emails(text)
    phones = extract_phones(text)
    website = extract_website(text, emails)
    domain = website or (domain_of(emails[0]) if emails else '')
    if domain in _GENERIC_DOMAINS:
        domain = ''

    designation = extract_designation(lines)
    company = extract_company(lines, domain)
    if not company and domain:
        # Nothing on the card reads as a company, but a work email domain
        # names one. A suggestion beats a blank field the reviewer has to
        # fill from the website they can already see.
        company = _company_from_domain(domain)
    name = extract_name(lines, designation, company)

    used = {l for l in (designation, company, name) if l}
    address = extract_address(lines, used)

    result.update({
        'name': name, 'designation': designation, 'company': company,
        'emails': emails,
        'phones': [p['value'] for p in phones],
        'phone_details': phones,
        'website': website, 'address': address,
    })
    return result


def normalise(model_output: dict) -> dict:
    """Force a model's answer into the contract, whatever it returned.

    The extractor promises strings and gets nulls, numbers, and one phone
    where the card had three. Everything downstream — duplicate matching,
    the review form, the Contact row — assumes the contract holds.
    """
    src = model_output if isinstance(model_output, dict) else {}
    out = blank()

    def text_of(*keys):
        for k in keys:
            v = src.get(k)
            if isinstance(v, (list, tuple)):
                v = v[0] if v else ''
            if v not in (None, ''):
                return str(v).strip()
        return ''

    def list_of(*keys):
        vals = []
        for k in keys:
            v = src.get(k)
            if isinstance(v, (list, tuple)):
                vals.extend(str(x).strip() for x in v if x)
            elif v not in (None, ''):
                # A model asked for one field sometimes returns
                # "a@x.com, b@x.com" or "a@x.com / b@x.com".
                vals.extend(p.strip() for p in re.split(r'[;,/|]', str(v))
                            if p.strip())
        seen, uniq = set(), []
        for v in vals:
            if v.lower() not in seen:
                seen.add(v.lower())
                uniq.append(v)
        return uniq

    out['name'] = text_of('name', 'full_name', 'person')
    out['designation'] = text_of('designation', 'title', 'job_title', 'role')
    out['company'] = text_of('company', 'organisation', 'organization',
                             'employer')
    out['emails'] = [e for e in list_of('emails', 'email', 'email_address')
                     if _EMAIL_RE.fullmatch(e)]
    out['phones'] = list_of('phones', 'mobile', 'telephone', 'phone',
                            'office_phone', 'direct')
    out['website'] = domain_of(text_of('website', 'url', 'web'))
    out['address'] = text_of('address', 'full_address')
    for extra in ('city', 'state', 'country', 'postal_code', 'pincode'):
        v = text_of(extra)
        if v and v.lower() not in out['address'].lower():
            out['address'] = (out['address'] + ', ' + v).strip(' ,')
    out['raw_text'] = text_of('raw_text')

    if not out['website'] and out['emails']:
        d = domain_of(out['emails'][0])
        if d and d not in _GENERIC_DOMAINS:
            out['website'] = d
    return out


def cross_check(primary: dict, fallback: dict) -> dict:
    """Merge a model's answer with the deterministic one.

    The model wins where it answered, because it can see the layout. The
    parser fills every gap and contributes anything the model missed —
    which is nearly always the second and third phone number. Fields
    where the two genuinely disagree are named in `conflicts` so the
    review screen can mark them rather than silently pick a side.
    """
    merged = blank()
    merged.update({k: v for k, v in (primary or {}).items() if k in merged})
    fb = fallback or {}
    conflicts = []

    for key in ('name', 'designation', 'company', 'website', 'address'):
        p, f = (merged.get(key) or '').strip(), (fb.get(key) or '').strip()
        if not p and f:
            merged[key] = f
        elif p and f and _flat(p) != _flat(f) and key != 'address':
            conflicts.append(key)

    for key in ('emails', 'phones'):
        seen, out = set(), []
        for v in list(merged.get(key) or []) + list(fb.get(key) or []):
            k = (normalise_phone(v) if key == 'phones'
                 else str(v).strip().lower())
            if k and k not in seen:
                seen.add(k)
                out.append(str(v).strip())
        merged[key] = out

    merged['raw_text'] = (merged.get('raw_text')
                          or fb.get('raw_text') or '')
    merged['conflicts'] = conflicts
    if fb.get('phone_details'):
        merged['phone_details'] = fb['phone_details']
    return merged


def _flat(s: str) -> str:
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def to_contact_fields(parsed: dict) -> dict:
    """Structured parse → the field names the CRM save endpoint expects.

    The Contact model holds one email and two numbers, so this picks the
    primary of each. The full lists stay on the review payload; nothing
    is dropped before a human has seen it.
    """
    emails = list(parsed.get('emails') or [])
    phones = list(parsed.get('phones') or [])
    details = parsed.get('phone_details') or []
    mobile, telephone = '', ''
    for d in details:
        if not mobile and d.get('label') in ('mobile', 'whatsapp'):
            mobile = d.get('value', '')
        elif not telephone and d.get('label') not in ('mobile', 'whatsapp',
                                                      'fax'):
            telephone = d.get('value', '')
    if not mobile and phones:
        mobile = phones[0]
    if not telephone:
        telephone = next((p for p in phones if p != mobile), '')

    return {
        'name': parsed.get('name', ''),
        'designation': parsed.get('designation', ''),
        'company': parsed.get('company', ''),
        'email': emails[0] if emails else '',
        'mobile': mobile,
        'telephone': telephone,
        'website': parsed.get('website', ''),
        'address': parsed.get('address', ''),
        'emails': emails,
        'phones': phones,
    }
