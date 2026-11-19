"""Content-first field scoring for a scanned card — CRM-SCAN-02.

The parser in `card_parse` reads a card top to bottom and takes the
first line that looks like each field. That fails on exactly the cards
that matter: the company printed only in the logo, the name below the
designation, a two-column layout read across. This module scores every
line the readers produced as a CANDIDATE for each field, from what the
line says rather than where it sits, and keeps the ranked list so the
review screen can offer the runners-up in a dropdown.

WHAT COUNTS AS EVIDENCE

company   the email domain's distinctive word on the line; a legal
          suffix (Pvt Ltd, Limited, LLP, GmbH…); an account already in
          the CRM with that name; what this domain was corrected to
          before (learned, `CardDomainHint`)
name      the email local part ("ravi.s" → a line with "Ravi" and an S
          word); a person-shaped line (two or three capitalised words,
          no digits, no company word); sitting next to the designation
designation  words from the title list (Manager, Director, Head, …)
state     the PIN code on the card, then a known city

Free and on-server: no service is called. Pure text, so the tests run
without a reader installed.
"""
from __future__ import annotations

import re

#: Legal and organisational suffixes. A line ending in one is a company.
LEGAL = ('private limited', 'pvt. ltd', 'pvt ltd', 'pvt. ltd.', 'p. ltd',
         'limited', 'ltd', 'llp', 'llc', 'inc', 'corporation', 'corp',
         'gmbh', 'ag', 'b.v.', 'bv', 's.p.a.', 'spa', 'plc', 'pte ltd',
         'co.', 'company', 'oy', 'ab', 'a/s', 'sa', 'srl', 'industries',
         'enterprises', 'group', 'logistics', 'engineering', 'works')

#: Words that make a line a job title.
TITLE_WORDS = (
    'manager', 'director', 'head', 'chief', 'officer', 'president',
    'vice', 'vp', 'general manager', 'gm', 'agm', 'dgm', 'executive',
    'engineer', 'lead', 'founder', 'co-founder', 'partner', 'proprietor',
    'owner', 'ceo', 'cfo', 'coo', 'cto', 'md', 'chairman', 'consultant',
    'specialist', 'analyst', 'coordinator', 'co-ordinator', 'supervisor',
    'incharge', 'in-charge', 'purchase', 'procurement', 'logistics',
    'sales', 'marketing', 'business development', 'operations',
    'commercial', 'secretary', 'associate', 'assistant', 'senior',
    'sr.', 'jr.', 'deputy', 'trainee', 'admin', 'administrator',
    'accountant', 'controller', 'architect', 'principal', 'buyer',
)

_DIGITS = re.compile(r'\d')
_EMAIL = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
_URL = re.compile(r'(?:https?://|www\.)\S+', re.I)

GENERIC_DOMAINS = frozenset((
    'gmail', 'yahoo', 'hotmail', 'outlook', 'rediffmail', 'live',
    'icloud', 'aol', 'protonmail', 'zoho', 'ymail', 'msn'))


def _lines(*texts):
    seen, out = set(), []
    for text in texts:
        for raw in str(text or '').splitlines():
            line = ' '.join(raw.strip().split()).strip(' |·•,;')
            key = line.lower()
            if len(line) >= 2 and key not in seen:
                seen.add(key)
                out.append(line)
    return out


def domain_word(email):
    """The distinctive word of an email domain: ravi@opterix.in → opterix."""
    text = str(email or '').lower()
    if '@' not in text:
        return ''
    host = text.split('@', 1)[1]
    parts = [p for p in host.split('.') if p]
    if len(parts) < 2:
        return ''
    core = parts[-3] if len(parts) >= 3 and parts[-2] in (
        'co', 'com', 'org', 'net', 'ac', 'gov') else parts[-2]
    return '' if core in GENERIC_DOMAINS else core


def _is_contact_line(line):
    low = line.lower()
    return bool(_EMAIL.search(line) or _URL.search(line)
                or len(_DIGITS.findall(line)) >= 6
                or re.match(r'^(tel|ph|mob|m|t|f|fax|e|w|email|web)\b[:.]', low))


def _has_title(line):
    low = ' ' + re.sub(r'[^a-z\- ]', ' ', line.lower()) + ' '
    return any(f' {w} ' in low for w in TITLE_WORDS)


def _legal(line):
    low = ' ' + re.sub(r'[^a-z/. ]', ' ', line.lower()).strip() + ' '
    return any(low.rstrip().endswith(' ' + s) or f' {s} ' in low
               for s in LEGAL)


def _person_shaped(line):
    if _DIGITS.search(line) or _is_contact_line(line) or _legal(line):
        return False
    words = [w for w in re.split(r'\s+', line.strip()) if w]
    words = [w for w in words if w.lower().strip('.') not in
             ('mr', 'mrs', 'ms', 'dr', 'shri', 'smt', 'er', 'capt')]
    if not 2 <= len(words) <= 4:
        return False
    return all(re.match(r"^[A-Z][A-Za-z'.\-]*$", w) or
               re.match(r'^[A-Z]\.?$', w) for w in words)


def _cand(out, value, score, why):
    value = (value or '').strip()
    if not value:
        return
    for c in out:
        if c['value'].lower() == value.lower():
            if score > c['score']:
                c['score'], c['why'] = score, why
            return
    out.append({'value': value, 'score': score, 'why': why})


def company_candidates(lines, email='', *, known=(), learned=''):
    """Ranked company candidates. `known` is account names in the CRM."""
    out = []
    word = domain_word(email)
    known_low = {str(k).lower(): k for k in known or ()}
    if learned:
        _cand(out, learned, 97, 'what this email domain was corrected to before')
    for i, line in enumerate(lines):
        if _is_contact_line(line) or _person_shaped(line) and not _legal(line):
            continue
        score, why = 0, []
        low = line.lower()
        if word and word in re.sub(r'[^a-z0-9]', '', low):
            score += 45
            why.append(f'has "{word}" from the email domain')
        if _legal(line):
            score += 35
            why.append('ends like a company name')
        if low in known_low:
            score += 40
            why.append('an account already in the CRM')
        if _has_title(line) and not _legal(line):
            score -= 30
        if line.isupper() and len(line) > 3:
            score += 5
        if i < 3:
            score += 5
        if score > 20:
            _cand(out, line, min(score, 96), '; '.join(why) or 'company-shaped')
    if word and not any(word in re.sub(r'[^a-z0-9]', '', c['value'].lower())
                        for c in out):
        _cand(out, word.title(), 45, 'worked out from the email domain')
    out.sort(key=lambda c: -c['score'])
    return out[:5]


def name_candidates(lines, email='', designation=''):
    out = []
    local = str(email or '').lower().split('@', 1)[0] if '@' in str(email or '') else ''
    bits = [b for b in re.split(r'[._\-0-9]+', local) if len(b) >= 2]
    titles = [i for i, l in enumerate(lines) if designation and
              l.lower() == designation.lower()]
    for i, line in enumerate(lines):
        if not _person_shaped(line) or _has_title(line):
            continue
        score, why = 50, ['looks like a person']
        words = [w.lower().strip('.') for w in line.split()]
        if bits and any(b in words or any(w.startswith(b) for w in words)
                        for b in bits):
            score += 35
            why.append('matches the email address')
        if titles and min(abs(i - t) for t in titles) == 1:
            score += 10
            why.append('next to the designation')
        _cand(out, line, min(score, 96), '; '.join(why))
    out.sort(key=lambda c: -c['score'])
    return out[:5]


def designation_candidates(lines):
    out = []
    for line in lines:
        if _is_contact_line(line) or _legal(line) or len(line) > 70:
            continue
        if _has_title(line):
            n = sum(1 for w in TITLE_WORDS if f' {w} ' in
                    ' ' + re.sub(r'[^a-z\- ]', ' ', line.lower()) + ' ')
            _cand(out, line, min(60 + 10 * n, 95), 'has job-title words')
    out.sort(key=lambda c: -c['score'])
    return out[:5]


def state_candidates(text, city=''):
    from app.services import india_places as ip

    out = []
    pin = ip.find_pin(text)
    if pin and ip.state_for_pin(pin):
        _cand(out, ip.state_for_pin(pin), 92, f'from the PIN code {pin}')
    if city and ip.state_for_city(city):
        _cand(out, ip.state_for_city(city), 60,
              f'worked out from the city {city}, not printed')
    out.sort(key=lambda c: -c['score'])
    return out


def known_accounts(word):
    """Account names that contain the email domain's word."""
    if not word:
        return []
    try:
        from app import Company
        return [a.name for a in Company.query
                .filter(Company.name.ilike(f'%{word}%')).limit(20).all()]
    except Exception:                                     # noqa: BLE001
        return []


def learned_company(email):
    try:
        from app.models.business_card import CardDomainHint
        word = (str(email or '').lower().split('@', 1)[1]
                if '@' in str(email or '') else '')
        if not word:
            return ''
        row = CardDomainHint.query.filter_by(domain=word,
                                             field='company').first()
        return row.value if row is not None else ''
    except Exception:                                     # noqa: BLE001
        return ''


def rescore(fields, *texts):
    """Add ranked candidates per field, and take the best where it beats
    what the top-to-bottom parser chose. Returns the fields dict.

    `fields['candidates']` = {field: [{value, score, why}]}
    `fields['confidence'][field]` gets the winner's score and reason.
    """
    from app.services import card_fields

    fields = dict(fields or {})
    lines = _lines(*texts, fields.get('raw_text') or '')
    email = fields.get('email') or ''
    cands = {}
    cands['company'] = company_candidates(
        lines, email, known=known_accounts(domain_word(email)),
        learned=learned_company(email))
    cands['designation'] = designation_candidates(lines)
    cands['name'] = name_candidates(
        lines, email, (cands['designation'][0]['value']
                       if cands['designation'] else fields.get('designation') or ''))
    cands['state'] = state_candidates('\n'.join(lines), fields.get('city') or '')

    conf = dict(fields.get('confidence') or {})
    for field, ranked in cands.items():
        current = (fields.get(field) or '').strip()
        if current:
            _cand(ranked, current, int((conf.get(field) or {}).get('score') or 60),
                  'what the reader chose')
            ranked.sort(key=lambda c: -c['score'])
        if not ranked:
            continue
        top = ranked[0]
        if top['value'] != current and (not current or top['score'] >= 75):
            fields[field] = top['value']
            if field == 'name':
                try:
                    split = card_fields.split_name(top['value'])
                    fields['first_name'] = split.get('first') or ''
                    fields['last_name'] = split.get('last') or ''
                except Exception:                         # noqa: BLE001
                    pass
        chosen = fields.get(field) or ''
        mine = next((c for c in ranked if c['value'] == chosen), top)
        before = conf.get(field) or {}
        if chosen == current and before.get('low'):
            # The reader already flagged this value (worked out, or the
            # passes disagreed). Agreeing with it is not new evidence.
            before = dict(before)
            before['alternatives'] = [c['value'] for c in ranked
                                      if c['value'] != chosen][:4]
            conf[field] = before
            continue
        conf[field] = {'score': mine['score'], 'why': mine['why'],
                       'low': mine['score'] < 70 or (len(ranked) > 1 and
                              ranked[1]['score'] >= mine['score'] - 10),
                       'empty': not chosen,
                       'alternatives': [c['value'] for c in ranked
                                        if c['value'] != chosen][:4]}
    fields['candidates'] = {k: v for k, v in cands.items() if v}
    fields['confidence'] = conf
    return fields


def learn(original, saved, *, actor=''):
    """Remember what this email domain's company was corrected to.

    Called on save. Only a CHANGE is learned — a card saved as read
    teaches nothing new — and only for a company email domain.
    """
    email = (saved or {}).get('email') or (original or {}).get('email') or ''
    if '@' not in str(email) or not domain_word(email):
        return False
    domain = str(email).lower().split('@', 1)[1]
    before = str((original or {}).get('company') or '').strip()
    after = str((saved or {}).get('company') or '').strip()
    if not after or after == before:
        return False
    from app import db
    from app.models.business_card import CardDomainHint
    row = CardDomainHint.query.filter_by(domain=domain, field='company').first()
    if row is None:
        row = CardDomainHint(domain=domain, field='company', value=after,
                             times=1, updated_by=actor or '')
        db.session.add(row)
    else:
        row.value, row.times, row.updated_by = after, (row.times or 0) + 1, actor or ''
    return True
