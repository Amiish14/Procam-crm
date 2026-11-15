"""The fields a card carries, each read as the thing it is.

CRM-SEARCH-SCAN-01 Part 2. `card_parse` finds WHERE on the card each
thing is; this turns what it found into what the CRM stores:

    phones      every number on the card, in E.164
    name        first and last as well as the whole
    address     city, state, country, postal code out of the block
    linkedin    the profile, however it was printed
    industry    only ever from the account it matched — never guessed

PHONE NUMBERS ARE E.164 OR THEY ARE NOT NUMBERS

`phonenumbers` is the library the spec names and the only honest way
to do this: a number is valid for a region or it is not, and the
region is a fact about the card, not about the server. Where the
library is not installed the fallback is deliberately narrow — an
Indian ten-digit number and anything already written with a "+" — and
it SAYS it is a fallback, because a number silently turned into the
wrong country's E.164 is a number nobody can ring.

COUNTRY AND STATE COME FROM A LIST, NOT FROM A GUESS

The Indian states are the ones already in the new-contact form. A
line that is not one of them is not a state, and the field is left
empty for the reviewer rather than filled with the nearest word — the
review form is always editable precisely so that an empty field costs
one keystroke and a wrong one costs a correction nobody makes.
"""
from __future__ import annotations

import re

#: The same list the new-contact form offers, plus the union
#: territories that appear on cards.
INDIAN_STATES = (
    'Andhra Pradesh', 'Arunachal Pradesh', 'Assam', 'Bihar',
    'Chandigarh', 'Chhattisgarh', 'Dadra and Nagar Haveli', 'Daman and Diu',
    'Delhi', 'Goa', 'Gujarat', 'Haryana', 'Himachal Pradesh',
    'Jammu and Kashmir', 'Jharkhand', 'Karnataka', 'Kerala', 'Ladakh',
    'Lakshadweep', 'Madhya Pradesh', 'Maharashtra', 'Manipur',
    'Meghalaya', 'Mizoram', 'Nagaland', 'Odisha', 'Puducherry',
    'Punjab', 'Rajasthan', 'Sikkim', 'Tamil Nadu', 'Telangana',
    'Tripura', 'Uttar Pradesh', 'Uttarakhand', 'West Bengal',
)

#: What a state is also printed as.
STATE_ALIASES = {
    'maharastra': 'Maharashtra', 'tamilnadu': 'Tamil Nadu',
    'orissa': 'Odisha', 'pondicherry': 'Puducherry',
    'new delhi': 'Delhi', 'ncr': 'Delhi', 'nct of delhi': 'Delhi',
    'uttaranchal': 'Uttarakhand', 'up': 'Uttar Pradesh',
    'mp': 'Madhya Pradesh', 'ap': 'Andhra Pradesh',
    'ts': 'Telangana', 'wb': 'West Bengal', 'tn': 'Tamil Nadu',
    'karnatak': 'Karnataka', 'gujrat': 'Gujarat',
}

#: Cities that name themselves on cards without a state beside them.
#: Only the ones that are unambiguous in India.
CITY_STATE = {
    'mumbai': 'Maharashtra', 'navi mumbai': 'Maharashtra',
    'thane': 'Maharashtra', 'pune': 'Maharashtra',
    'nagpur': 'Maharashtra', 'nashik': 'Maharashtra',
    'delhi': 'Delhi', 'new delhi': 'Delhi', 'noida': 'Uttar Pradesh',
    'gurgaon': 'Haryana', 'gurugram': 'Haryana', 'faridabad': 'Haryana',
    'ghaziabad': 'Uttar Pradesh', 'chennai': 'Tamil Nadu',
    'coimbatore': 'Tamil Nadu', 'bengaluru': 'Karnataka',
    'bangalore': 'Karnataka', 'mysuru': 'Karnataka',
    'hyderabad': 'Telangana', 'secunderabad': 'Telangana',
    'kolkata': 'West Bengal', 'howrah': 'West Bengal',
    'ahmedabad': 'Gujarat', 'surat': 'Gujarat', 'vadodara': 'Gujarat',
    'rajkot': 'Gujarat', 'gandhinagar': 'Gujarat',
    'jamnagar': 'Gujarat', 'bhavnagar': 'Gujarat', 'mundra': 'Gujarat',
    'kandla': 'Gujarat', 'hazira': 'Gujarat',
    'jaipur': 'Rajasthan', 'jodhpur': 'Rajasthan', 'udaipur': 'Rajasthan',
    'lucknow': 'Uttar Pradesh', 'kanpur': 'Uttar Pradesh',
    'varanasi': 'Uttar Pradesh', 'patna': 'Bihar',
    'bhubaneswar': 'Odisha', 'cuttack': 'Odisha', 'paradip': 'Odisha',
    'raipur': 'Chhattisgarh', 'bhopal': 'Madhya Pradesh',
    'indore': 'Madhya Pradesh', 'jabalpur': 'Madhya Pradesh',
    'visakhapatnam': 'Andhra Pradesh', 'vizag': 'Andhra Pradesh',
    'vijayawada': 'Andhra Pradesh', 'kochi': 'Kerala',
    'cochin': 'Kerala', 'ernakulam': 'Kerala',
    'thiruvananthapuram': 'Kerala', 'kozhikode': 'Kerala',
    'guwahati': 'Assam', 'ranchi': 'Jharkhand',
    'jamshedpur': 'Jharkhand', 'dhanbad': 'Jharkhand',
    'ludhiana': 'Punjab', 'amritsar': 'Punjab', 'chandigarh': 'Chandigarh',
    'dehradun': 'Uttarakhand', 'shimla': 'Himachal Pradesh',
    'goa': 'Goa', 'panaji': 'Goa', 'vasco': 'Goa',
}

#: A country printed on a card, and what the CRM calls it.
COUNTRY_WORDS = {
    'india': 'India', 'bharat': 'India',
    'uae': 'United Arab Emirates', 'u.a.e': 'United Arab Emirates',
    'united arab emirates': 'United Arab Emirates',
    'dubai': 'United Arab Emirates', 'abu dhabi': 'United Arab Emirates',
    'sharjah': 'United Arab Emirates',
    'saudi arabia': 'Saudi Arabia', 'ksa': 'Saudi Arabia',
    'oman': 'Oman', 'muscat': 'Oman', 'qatar': 'Qatar', 'doha': 'Qatar',
    'bahrain': 'Bahrain', 'kuwait': 'Kuwait',
    'singapore': 'Singapore', 'malaysia': 'Malaysia',
    'indonesia': 'Indonesia', 'thailand': 'Thailand',
    'vietnam': 'Vietnam', 'china': 'China', 'hong kong': 'Hong Kong',
    'japan': 'Japan', 'south korea': 'South Korea', 'korea': 'South Korea',
    'taiwan': 'Taiwan', 'sri lanka': 'Sri Lanka', 'bangladesh': 'Bangladesh',
    'nepal': 'Nepal', 'pakistan': 'Pakistan',
    'usa': 'United States', 'u.s.a': 'United States',
    'united states': 'United States', 'america': 'United States',
    'uk': 'United Kingdom', 'united kingdom': 'United Kingdom',
    'england': 'United Kingdom', 'germany': 'Germany',
    'netherlands': 'Netherlands', 'belgium': 'Belgium',
    'france': 'France', 'italy': 'Italy', 'spain': 'Spain',
    'turkey': 'Turkey', 'egypt': 'Egypt', 'south africa': 'South Africa',
    'kenya': 'Kenya', 'nigeria': 'Nigeria', 'australia': 'Australia',
    'brazil': 'Brazil', 'canada': 'Canada', 'mexico': 'Mexico',
}

#: Dial code → the region `phonenumbers` wants, for a "+" number.
_REGION_BY_COUNTRY = {
    'India': 'IN', 'United Arab Emirates': 'AE', 'Saudi Arabia': 'SA',
    'Oman': 'OM', 'Qatar': 'QA', 'Bahrain': 'BH', 'Kuwait': 'KW',
    'Singapore': 'SG', 'Malaysia': 'MY', 'Indonesia': 'ID',
    'Thailand': 'TH', 'Vietnam': 'VN', 'China': 'CN',
    'Hong Kong': 'HK', 'Japan': 'JP', 'South Korea': 'KR',
    'Taiwan': 'TW', 'Sri Lanka': 'LK', 'Bangladesh': 'BD',
    'Nepal': 'NP', 'Pakistan': 'PK', 'United States': 'US',
    'United Kingdom': 'GB', 'Germany': 'DE', 'Netherlands': 'NL',
    'Belgium': 'BE', 'France': 'FR', 'Italy': 'IT', 'Spain': 'ES',
    'Turkey': 'TR', 'Egypt': 'EG', 'South Africa': 'ZA',
    'Kenya': 'KE', 'Nigeria': 'NG', 'Australia': 'AU',
    'Brazil': 'BR', 'Canada': 'CA', 'Mexico': 'MX',
}

_PIN_RE = re.compile(r'\b(\d{6})\b')
_ZIP_RE = re.compile(r'\b(\d{5}(?:-\d{4})?)\b')
_LINKEDIN_RE = re.compile(
    r'(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/'
    r'(?:in|company|pub)/([A-Za-z0-9._%-]+)', re.I)

#: Printed before a surname, not part of one.
NAME_PREFIXES = ('mr', 'mrs', 'ms', 'miss', 'dr', 'prof', 'er', 'capt',
                 'col', 'maj', 'shri', 'smt', 'sri', 'ca', 'cs')

#: Printed after a name and not part of it.
NAME_SUFFIXES = ('jr', 'sr', 'ii', 'iii', 'iv', 'phd', 'ph.d', 'mba',
                 'pmp', 'cpa', 'bcom', 'mcom', 'be', 'btech', 'mtech')


def region_for(country='', hint='India'):
    """The region a bare number on this card belongs to."""
    name = COUNTRY_WORDS.get(str(country or '').strip().lower(),
                             (country or '').strip())
    return _REGION_BY_COUNTRY.get(name) or _REGION_BY_COUNTRY.get(hint) \
        or 'IN'


def phone_library():
    """`phonenumbers`, or None. Named so the callers can say which."""
    try:
        import phonenumbers
        return phonenumbers
    except Exception:                                     # noqa: BLE001
        return None


def to_e164(value, *, country='', hint='India'):
    """One number → ('+91…', 'mobile'|'fixed'|'', how it was decided).

    The third value is how sure this is, in words, because the review
    screen shows it: a number the library validated for its region is
    a different kind of fact from ten digits that looked Indian.
    """
    raw = str(value or '').strip()
    if not raw:
        return '', '', ''
    digits = re.sub(r'[^\d+]', '', raw.replace('(0)', ''))
    if digits.startswith('00'):
        digits = '+' + digits[2:]
    lib = phone_library()
    region = region_for(country, hint)
    if lib is not None:
        try:
            parsed = lib.parse(digits if digits.startswith('+') else raw,
                               None if digits.startswith('+') else region)
            if lib.is_valid_number(parsed):
                kind = lib.number_type(parsed)
                label = ('mobile' if kind == lib.PhoneNumberType.MOBILE
                         else ('fixed' if kind in (
                             lib.PhoneNumberType.FIXED_LINE,
                             lib.PhoneNumberType.FIXED_LINE_OR_MOBILE)
                             else ''))
                return (lib.format_number(
                    parsed, lib.PhoneNumberFormat.E164), label,
                    f'valid for {lib.region_code_for_number(parsed) or region}')
            return '', '', 'not a valid number for this region'
        except Exception:                                 # noqa: BLE001
            return '', '', 'could not be read as a number'
    # Without the library: only what can be stated without guessing.
    if digits.startswith('+') and 8 <= len(re.sub(r'\D', '', digits)) <= 15:
        return digits, '', 'written with a country code'
    bare = re.sub(r'\D', '', digits)
    if region == 'IN' and len(bare) == 10 and bare[0] in '6789':
        return '+91' + bare, 'mobile', 'ten Indian digits'
    if region == 'IN' and len(bare) == 11 and bare.startswith('0'):
        return '+91' + bare[1:], '', 'eleven digits beginning 0'
    return '', '', 'no country code, and not a ten-digit Indian number'


def all_phones(values, *, country='', hint='India'):
    """Every number on the card, de-duplicated on the E.164 form.

    A number that cannot be put in E.164 is KEPT, marked, and shown to
    the reviewer as it was printed. Dropping it would lose the only
    way of reaching somebody because a library disagreed with a card.
    """
    out, seen = [], set()
    for raw in values or []:
        text = str(raw or '').strip()
        if not text:
            continue
        e164, kind, why = to_e164(text, country=country, hint=hint)
        key = e164 or re.sub(r'\D', '', text)[-10:] or text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({'printed': text, 'e164': e164, 'kind': kind,
                    'why': why, 'usable': bool(e164)})
    return out


def split_name(full):
    """A whole name into first and last, with the title taken off.

    Indian cards very often print "SURNAME Firstname" or three given
    names; this keeps the WHOLE name as the authority and offers the
    two halves, which the reviewer can swap in one edit.
    """
    text = ' '.join(str(full or '').replace(',', ' ').split())
    if not text:
        return {'full': '', 'first': '', 'last': ''}
    parts = [p for p in text.split(' ') if p]
    while parts and parts[0].strip('.').lower() in NAME_PREFIXES:
        parts.pop(0)
    while parts and parts[-1].strip('.').lower() in NAME_SUFFIXES:
        parts.pop()
    if not parts:
        return {'full': text, 'first': '', 'last': ''}
    if len(parts) == 1:
        return {'full': ' '.join(parts), 'first': parts[0], 'last': ''}
    return {'full': ' '.join(parts), 'first': parts[0],
            'last': parts[-1]}


#: A line that names the department rather than the job.
DEPARTMENT_WORDS = ('department', 'dept', 'division', 'desk', 'cell',
                    'vertical', 'group', 'business unit', 'sbu')


def split_designation(designation, lines=()):
    """The job title and the department, which are usually one line.

    "Senior Manager - Logistics" is a title and a department; so is
    "DGM (Supply Chain)". The title keeps the whole string, because
    that is what the card says and what the reviewer recognises; the
    department is offered beside it.
    """
    text = ' '.join(str(designation or '').split())
    department = ''
    if text:
        found = re.split(r'\s*[-–—|,/]\s*|\s*[(\[]', text, 1)
        if len(found) > 1:
            tail = found[1].strip(' )]')
            head = found[0].strip()
            if tail and len(tail) > 2 and head:
                department = tail
    if not department:
        for line in lines or ():
            low = str(line or '').lower()
            if any(w in low for w in DEPARTMENT_WORDS) and len(line) < 60:
                department = re.sub(r'(?i)\b(department|dept\.?|division)\b',
                                    '', str(line)).strip(' -:,|')
                break
    return {'designation': text, 'department': department}


def linkedin_of(*texts):
    for text in texts:
        found = _LINKEDIN_RE.search(str(text or ''))
        if found:
            whole = found.group(0)
            if not whole.lower().startswith('http'):
                whole = 'https://' + whole.lstrip('/')
            return whole.rstrip('/.,;')
    return ''


def _titled(word):
    return ' '.join(w.capitalize() if w.islower() or w.isupper() else w
                    for w in word.split())


def split_address(address, *, raw_text=''):
    """An address block into city / state / country / postal code.

    Read from the end backwards, which is the order an address is
    printed in, and each piece only when it is RECOGNISED: a state
    from the list, a country from the list, a postal code by its
    shape. What is left is the street. A line that matches nothing
    stays in the street and the reviewer places it — the form is
    always editable, so a blank field is a keystroke and a wrong one
    is a correction nobody makes.
    """
    text = ' '.join(str(address or '').split())
    out = {'address': text, 'city': '', 'state': '', 'country': '',
           'postal_code': '', 'street': text}
    haystack = (text + '\n' + str(raw_text or '')).lower()

    for word, name in sorted(COUNTRY_WORDS.items(), key=lambda kv: -len(kv[0])):
        if re.search(r'(?<![a-z])' + re.escape(word) + r'(?![a-z])',
                     haystack):
            out['country'] = name
            break

    for state in sorted(INDIAN_STATES, key=len, reverse=True):
        if re.search(r'(?<![a-z])' + re.escape(state.lower()) + r'(?![a-z])',
                     haystack):
            out['state'] = state
            break
    if not out['state']:
        for alias, state in sorted(STATE_ALIASES.items(),
                                   key=lambda kv: -len(kv[0])):
            if len(alias) <= 3:
                continue        # "up", "ap" are too common to match loose
            if re.search(r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])',
                         haystack):
                out['state'] = state
                break

    for city, state in sorted(CITY_STATE.items(), key=lambda kv: -len(kv[0])):
        if re.search(r'(?<![a-z])' + re.escape(city) + r'(?![a-z])',
                     haystack):
            out['city'] = _titled(city)
            if not out['state']:
                out['state'] = state
            if not out['country']:
                out['country'] = 'India'
            break

    pin = _PIN_RE.search(text) or _PIN_RE.search(str(raw_text or ''))
    if pin and (out['country'] in ('India', '') or not out['country']):
        out['postal_code'] = pin.group(1)
        if not out['country'] and out['state']:
            out['country'] = 'India'
    elif out['country'] == 'United States':
        zipped = _ZIP_RE.search(text)
        if zipped:
            out['postal_code'] = zipped.group(1)

    street = text
    for piece in (out['postal_code'], out['state'], out['country']):
        if piece:
            street = re.sub(r'(?i)(?<![a-z])' + re.escape(piece) + r'(?![a-z])',
                            ' ', street)
    out['street'] = ' '.join(street.replace(' ,', ',').split()).strip(' ,-')
    return out


def enrich(fields, *, raw_text='', country_hint='India'):
    """Everything above, applied to one extraction. Never raises.

    Returns a NEW dict: the caller keeps what the reader said as well,
    because the review screen shows both and the reviewer decides.
    """
    out = dict(fields or {})
    name = split_name(out.get('name') or '')
    out['name'] = name['full'] or (out.get('name') or '')
    out['first_name'] = name['first']
    out['last_name'] = name['last']

    where = split_address(out.get('address') or '', raw_text=raw_text)
    for key in ('city', 'state', 'country', 'postal_code'):
        if not str(out.get(key) or '').strip():
            out[key] = where[key]
    out['street'] = where['street']

    numbers = list(out.get('phones') or [])
    for key in ('mobile', 'telephone'):
        if out.get(key):
            numbers.append(out[key])
    detailed = all_phones(numbers, country=out.get('country') or '',
                          hint=country_hint)
    out['phone_numbers'] = detailed
    out['phones'] = [p['e164'] or p['printed'] for p in detailed]
    def shown(entry):
        return entry['e164'] or entry['printed']

    mobiles = [p for p in detailed if p['kind'] == 'mobile']
    others = [p for p in detailed if p['kind'] != 'mobile']
    if mobiles:
        out['mobile'] = shown(mobiles[0])
        rest = mobiles[1:] + others
    elif detailed:
        out['mobile'] = shown(detailed[0])
        rest = detailed[1:]
    else:
        rest = []
    out['telephone'] = shown(rest[0]) if rest \
        else (out.get('telephone') or '')

    job = split_designation(out.get('designation') or '',
                            str(raw_text or '').splitlines())
    out['designation'] = job['designation']
    if not str(out.get('department') or '').strip():
        out['department'] = job['department']

    out['linkedin_url'] = (out.get('linkedin_url')
                           or out.get('linkedin')
                           or linkedin_of(out.get('website') or '', raw_text))
    out.setdefault('department', '')
    out.setdefault('industry', '')
    return out
