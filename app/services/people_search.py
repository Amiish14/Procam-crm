"""One row per person, from the contacts AND the leads.

CRM-SEARCH-SCAN-01 Part 1, with the decision of 15 Nov: **the real
contact data is on the leads**. 667 contacts against 12,000 leads
carrying a PIC, an email, a phone, a company and a vertical — a
contact search that reads only `contacts` answers "we don't know
them" about people we have been quoting for months.

So a result row is a PERSON, assembled from two places:

    Contact        the people master
    Lead.pic       the person an enquiry is with

and the two are DE-DUPLICATED on email and phone, because the same
person is usually both. The row says which it came from — "Contact",
or "From lead #12644" — so nobody has to guess why a name has no
designation on it.

FILTERS ARE ANDed, AND EACH ONE READS SEVERAL COLUMNS

    name        Contact.name, Lead.pic
    company     Contact.company, the linked account's name AND its
                aliases, Lead.company
    country     Contact.country, the account's, the lead's — and, when
                none is filled, the dialling code of the phone
    location    city and state on all three
    vertical    the DESK: Lead.procam_vertical and Company.vertical,
                through `vertical_master.canonical`, so one spelling
                matches thirteen
    industry    what the customer DOES: Contact.industry,
                Company.industry. Deliberately a different filter from
                the vertical — `app.py:1294` says why.

VISIBILITY IS NOT NEGOTIABLE

Contacts come through `scope.contacts` and leads through
`scope.leads`, the same two functions every other screen uses. A
search that widened what somebody can see would be a data leak with a
search box on it.

SPEED, AT THIS SIZE

667 + 12,000 rows. The filters run in SQL, the merge runs in Python
over what they return, and the whole thing is measured in the
response (`took_ms`). FTS5 was offered and declined for this size,
and the decision was right: the plan below is a scan of a small table
and the correctness is readable. If the book grows an order of
magnitude, the place to change is `_candidates`.
"""
from __future__ import annotations

import re
import time

#: How many rows each source may contribute before paging. Bounded so
#: one unfiltered search cannot pull the whole book into memory.
CAP = 4000

#: What a page holds.
PER_PAGE = 50

#: Dialling code → country, for the fallback when the country field is
#: blank. Only the ones this book actually carries; an unknown code is
#: left blank rather than guessed.
DIAL_CODES = {
    '91': 'India', '971': 'United Arab Emirates', '966': 'Saudi Arabia',
    '974': 'Qatar', '968': 'Oman', '973': 'Bahrain', '965': 'Kuwait',
    '1': 'United States', '44': 'United Kingdom', '49': 'Germany',
    '33': 'France', '39': 'Italy', '31': 'Netherlands', '34': 'Spain',
    '46': 'Sweden', '47': 'Norway', '45': 'Denmark', '41': 'Switzerland',
    '43': 'Austria', '32': 'Belgium', '351': 'Portugal', '48': 'Poland',
    '90': 'Turkey', '7': 'Russia', '86': 'China', '81': 'Japan',
    '82': 'South Korea', '65': 'Singapore', '60': 'Malaysia',
    '66': 'Thailand', '62': 'Indonesia', '63': 'Philippines',
    '84': 'Vietnam', '852': 'Hong Kong', '886': 'Taiwan',
    '61': 'Australia', '64': 'New Zealand', '27': 'South Africa',
    '20': 'Egypt', '234': 'Nigeria', '254': 'Kenya', '55': 'Brazil',
    '52': 'Mexico', '54': 'Argentina', '56': 'Chile', '57': 'Colombia',
    '92': 'Pakistan', '880': 'Bangladesh', '94': 'Sri Lanka',
    '977': 'Nepal', '95': 'Myanmar', '98': 'Iran', '964': 'Iraq',
    '962': 'Jordan', '961': 'Lebanon', '972': 'Israel',
}

#: Longest first, so +9714… reads as 971 and not as 9.
_CODES = sorted(DIAL_CODES, key=len, reverse=True)


def country_from_phone(*numbers):
    """The country a "+" number belongs to, or ''.

    Only for a number written in international form. A ten-digit
    Indian mobile with no "+" says nothing: it is what every number in
    this book looks like, and reading India into it would make the
    country filter agree with itself.
    """
    for raw in numbers:
        text = str(raw or '').strip()
        if not text.startswith('+'):
            # "0091" is the same statement as "+91".
            if text.startswith('00'):
                text = '+' + text[2:]
            else:
                continue
        digits = re.sub(r'\D', '', text)
        for code in _CODES:
            if digits.startswith(code):
                return DIAL_CODES[code]
    return ''


#: Local parts that are a robot whatever the domain sends them.
#: `partner@`, `rfq@` and the like are NOT here: those are mailboxes a
#: human reads. These are addresses nobody is.
SYSTEM_LOCALS = frozenset((
    'noreply', 'no-reply', 'no_reply', 'donotreply', 'do-not-reply',
    'do_not_reply', 'notifications', 'notification', 'notify',
    'mailer-daemon', 'mailerdaemon', 'postmaster', 'bounce', 'bounces',
    'automailer', 'autoreply', 'auto-reply', 'alerts', 'alert',
    'system', 'noreplyemail',
))


def is_system_address(address):
    """The portal or robot this address belongs to, or ''.

    UAT of 15 Nov: `partner@procol.in` was shown as a person under
    Amns. It is the portal's own delivery address — the same thing
    `account_match.not_a_customer` refuses to learn — and a search
    that offers it as somebody to ring is offering a robot.

    The portal half is `portal_rfq.is_portal_sender`, deliberately the
    SAME list the matcher uses (`data/portal_senders.txt` plus the
    brand labels), so a portal added there is excluded here without a
    second edit.
    """
    text = str(address or '').strip().lower().strip('<>,;')
    if '@' not in text:
        return ''
    try:
        from app.services import portal_rfq
        portal = portal_rfq.is_portal_sender(text)
    except Exception:                                     # noqa: BLE001
        portal = ''
    if portal:
        return portal
    local, _, domain = text.partition('@')
    local = re.split(r'[+]', local, 1)[0]
    if local in SYSTEM_LOCALS:
        return domain or local
    return ''


def _real_emails(*values):
    """The addresses that belong to a PERSON."""
    out, bad = [], []
    for raw in values:
        text = str(raw or '').strip()
        if not text:
            continue
        (bad if is_system_address(text) else out).append(text)
    return out, bad


def _is_a_robot(row):
    """A result row that is an address, not a person.

    Two shapes, both seen on live. The lead whose PIC *is* the portal
    address (#12645, "partner@procol.in" — there is no person there to
    name), and the row whose only way of being reached was a portal
    address, which leaves nothing a search result is for. A row with a
    real phone or a second real address survives with the robot
    address dropped: the person is still a person.
    """
    person = (row.get('person') or '').strip()
    if '@' in person and is_system_address(person):
        return True
    if row.get('system_emails') and not row.get('emails') \
            and not row.get('phones'):
        return True
    return False


def _digits(value):
    """A phone as comparable digits — the last ten, which is what two
    spellings of one Indian number have in common."""
    found = re.sub(r'\D', '', str(value or ''))
    return found[-10:] if len(found) >= 10 else ''


def _email_key(value):
    text = str(value or '').strip().lower()
    return text if '@' in text else ''


def _like(value):
    return '%' + str(value or '').strip() + '%'


def _account_ids_for(company):
    """Accounts whose name OR alias matches — reusing the matcher.

    The alias reading is the one built on 14 Nov for the portal
    buyers: "Amns" answers to "ArcelorMittal Nippon Steel". A company
    filter that could not see an alias would miss every lead filed
    under the short name.
    """
    from app import Company
    from app.services import account_match, company_match

    want = company_match.norm(company or '')
    text = (company or '').strip().lower()
    if not text:
        return []
    found = set()
    try:
        rows = account_match.active_index()
    except Exception:                                     # noqa: BLE001
        rows = []
    for account, norms, _stricts, _own in rows:
        if any(text in str(n or '').lower() for n in (account.name or '',)):
            found.add(account.id)
            continue
        if want and want in norms:
            found.add(account.id)
            continue
        for alias in (getattr(account, 'aliases', None) or []):
            if text in str(alias).lower():
                found.add(account.id)
                break
    if not found:
        try:
            for account in (Company.query
                            .filter(Company.name.ilike(_like(text)))
                            .limit(200).all()):
                found.add(account.id)
        except Exception:                                 # noqa: BLE001
            pass
    return sorted(found)


def _account_ids_by_location(text):
    """Accounts in this city or state.

    The location filter has to reach the ACCOUNT's address: a contact
    often carries no city of its own, and the thing somebody means by
    "who do we know in Chennai" is the company's address as much as
    the person's.
    """
    from app import Company
    from sqlalchemy import or_

    text = (text or '').strip()
    if not text:
        return []
    like = _like(text)
    try:
        return [a.id for a in Company.query
                .filter(or_(Company.city.ilike(like),
                            Company.state.ilike(like)))
                .limit(2000).all()]
    except Exception:                                     # noqa: BLE001
        return []


def _contact_rows(f, sc):
    from app import Company, Contact, db
    from sqlalchemy import or_
    from app.access import scope as sc_mod

    q = sc_mod.contacts(Contact.query, sc=sc)
    if f['name']:
        q = q.filter(Contact.name.ilike(_like(f['name'])))
    if f['company']:
        ids = _account_ids_for(f['company'])
        clause = [Contact.company.ilike(_like(f['company']))]
        if ids:
            clause.append(Contact.company_id.in_(ids))
            clause.append(Contact.account_id.in_(ids))
        q = q.filter(or_(*clause))
    if f['industry']:
        q = q.filter(Contact.industry.ilike(_like(f['industry'])))
    if f['location']:
        like = _like(f['location'])
        clause = [Contact.city.ilike(like), Contact.state.ilike(like)]
        ids = _account_ids_by_location(f['location'])
        if ids:
            clause.append(Contact.company_id.in_(ids))
            clause.append(Contact.account_id.in_(ids))
        q = q.filter(or_(*clause))
    # Country and vertical are decided after the account is read, so
    # they are filtered in `_shape` where the fallbacks live.
    return q.limit(CAP).all()


def _lead_rows(f, sc):
    from app import Lead
    from sqlalchemy import or_
    from app.access import scope as sc_mod
    from app.services import vertical_master

    q = sc_mod.leads(
        Lead.query.filter(Lead.is_archived.isnot(True),
                          Lead.junk_deleted.isnot(True),
                          Lead.merged_into_id.is_(None)), sc=sc)
    # A lead with no person on it is not a person.
    q = q.filter(Lead.pic.isnot(None), Lead.pic != '')
    if f['name']:
        q = q.filter(Lead.pic.ilike(_like(f['name'])))
    if f['company']:
        ids = _account_ids_for(f['company'])
        clause = [Lead.company.ilike(_like(f['company']))]
        if ids:
            clause.append(Lead.company_id.in_(ids))
        q = q.filter(or_(*clause))
    if f['location']:
        like = _like(f['location'])
        clause = [Lead.city.ilike(like), Lead.state.ilike(like)]
        ids = _account_ids_by_location(f['location'])
        if ids:
            clause.append(Lead.company_id.in_(ids))
        q = q.filter(or_(*clause))
    if f['vertical']:
        want = vertical_master.canonical(f['vertical']) or f['vertical']
        # Every spelling that canonicalises to the wanted desk. Asked
        # of the DATA, so a spelling nobody has normalised yet still
        # answers the filter.
        from app import db
        spellings = [v for (v,) in db.session.query(
            Lead.procam_vertical).distinct().all()
            if v and vertical_master.canonical(v) == want]
        if spellings:
            q = q.filter(Lead.procam_vertical.in_(spellings))
        else:
            q = q.filter(Lead.procam_vertical.ilike(_like(f['vertical'])))
    return q.order_by(Lead.id.desc()).limit(CAP).all()


def _accounts_by_id(ids):
    from app import Company

    ids = [i for i in ids if i]
    if not ids:
        return {}
    return {a.id: a for a in Company.query.filter(Company.id.in_(ids)).all()}


def _shape_contact(row, accounts):
    account = accounts.get(row.account_id or row.company_id or 0)
    from app.services import vertical_master

    phones = [p for p in (row.mobile, row.phone) if p]
    country = (row.country or '').strip() or (
        (account.country or '').strip() if account is not None else '')
    if not country:
        country = country_from_phone(*phones)
        country_from = 'the phone code' if country else ''
    else:
        country_from = ''
    location = ' · '.join(
        x for x in ((row.city or '').strip(), (row.state or '').strip())
        if x) or (' · '.join(
            x for x in (((account.city or '') if account else ''),
                        ((account.state or '') if account else ''))
            if x) if account is not None else '')
    return {
        'kind': 'contact',
        'label': 'Contact',
        'id': row.id,
        'person': row.name or '',
        'company': (row.company or '')
                   or (account.name if account is not None else ''),
        'account_id': account.id if account is not None else None,
        'designation': row.designation or '',
        'country': country,
        'country_from': country_from,
        'location': location,
        'vertical': (vertical_master.canonical(account.vertical)
                     if account is not None else '') or '',
        'industry': (row.industry or '')
                    or ((account.industry or '') if account else ''),
        'phones': phones,
        'emails': _real_emails(row.email)[0],
        'system_emails': _real_emails(row.email)[1],
        'owner': row.assigned_to or '',
        'last_activity': str(row.created_at or '')[:10],
        'url': (f'/company/{account.id}' if account is not None else ''),
        'contact_id': row.id,
        # Where to open THIS PERSON, which is not the same as where to
        # open their company. UAT 17 Nov: a contact with no account
        # carried no url at all, so the duplicate list had nothing to
        # click and "use one of them" could not be done.
        'person_url': f'/app?contact={row.id}',
    }


def _shape_lead(row, accounts):
    from app.services import vertical_master

    account = accounts.get(row.company_id or 0)
    phones = [p for p in (getattr(row, 'phone', None),
                          getattr(row, 'phone2', None)) if p]
    country = (getattr(row, 'country', '') or '').strip()
    country_from = ''
    if not country and account is not None:
        country = (account.country or '').strip()
    if not country:
        country = country_from_phone(*phones)
        country_from = 'the phone code' if country else ''
    location = ' · '.join(
        x for x in ((row.city or '').strip(), (row.state or '').strip())
        if x)
    if not location and account is not None:
        location = ' · '.join(
            x for x in ((account.city or ''), (account.state or '')) if x)
    return {
        'kind': 'lead',
        'label': f'From lead #{row.id}',
        'id': row.id,
        'person': row.pic or '',
        'company': (row.company or '')
                   or (account.name if account is not None else ''),
        'account_id': account.id if account is not None else None,
        'designation': '',
        'country': country,
        'country_from': country_from,
        'location': location,
        'vertical': vertical_master.canonical(row.procam_vertical) or '',
        'industry': (getattr(row, 'industry', '') or '')
                    or ((account.industry or '') if account else ''),
        'phones': phones,
        'emails': _real_emails(row.email)[0],
        'system_emails': _real_emails(row.email)[1],
        'owner': row.assigned_to or '',
        'last_activity': str(getattr(row, 'updated_at', None)
                             or getattr(row, 'created_at', None) or '')[:10],
        'url': f'/?lead={row.id}',
        'person_url': f'/app?lead={row.id}',
        'lead_id': row.id,
    }


def _merge(rows):
    """One row per person. A contact wins; a lead fills its gaps.

    Keyed on email first and phone second, because those are the two
    things that are the same person twice. NOT on the name: two
    "Rajesh Kumar"s at two companies are two people, and a search that
    merged them would hide one of them completely.
    """
    out = []
    by_email, by_phone = {}, {}

    def absorb(into, extra):
        into['sources'] = into.get('sources', []) + [extra['label']]
        into['also_leads'] = into.get('also_leads', 0) + (
            1 if extra['kind'] == 'lead' else 0)
        for key in ('designation', 'country', 'location', 'vertical',
                    'industry', 'owner', 'company'):
            if not into.get(key) and extra.get(key):
                into[key] = extra[key]
        for key in ('phones', 'emails'):
            seen = {str(v).lower() for v in into.get(key) or []}
            for value in extra.get(key) or []:
                if str(value).lower() not in seen:
                    into[key] = (into.get(key) or []) + [value]
                    seen.add(str(value).lower())
        if extra['kind'] == 'lead' and not into.get('lead_id'):
            into['lead_id'] = extra['id']
            into['lead_url'] = extra['url']
        if not into.get('person_url') and extra.get('person_url'):
            into['person_url'] = extra['person_url']

    # Contacts first, so a contact is always the surviving row.
    for row in sorted(rows, key=lambda r: 0 if r['kind'] == 'contact' else 1):
        keys_e = [_email_key(e) for e in row['emails']]
        keys_p = [_digits(p) for p in row['phones']]
        held = None
        for key in keys_e:
            if key and key in by_email:
                held = by_email[key]
                break
        if held is None:
            for key in keys_p:
                if key and key in by_phone:
                    held = by_phone[key]
                    break
        if held is not None:
            absorb(held, row)
        else:
            row['sources'] = [row['label']]
            row.setdefault('also_leads', 0)
            out.append(row)
            held = row
        for key in keys_e:
            if key:
                by_email.setdefault(key, held)
        for key in keys_p:
            if key:
                by_phone.setdefault(key, held)
    return out


SORTS = {
    'name': lambda r: (r['person'] or '').lower(),
    'company': lambda r: (r['company'] or '').lower(),
    'country': lambda r: (r['country'] or '').lower(),
    'location': lambda r: (r['location'] or '').lower(),
    'vertical': lambda r: (r['vertical'] or '').lower(),
    'owner': lambda r: (r['owner'] or '').lower(),
    'last_activity': lambda r: (r['last_activity'] or ''),
}


def search(*, name='', company='', country='', location='', vertical='',
           industry='', sc=None, page=1, per_page=PER_PAGE,
           sort='name', descending=False):
    """The people matching every field given. Empty fields are ignored."""
    from app.services import vertical_master

    started = time.time()
    f = {'name': (name or '').strip(), 'company': (company or '').strip(),
         'country': (country or '').strip(),
         'location': (location or '').strip(),
         'vertical': (vertical or '').strip(),
         'industry': (industry or '').strip()}
    asked = {k: v for k, v in f.items() if v}

    rows = []
    contacts = _contact_rows(f, sc)
    leads = _lead_rows(f, sc)
    # The cap is a promise about memory, not about the answer. If
    # either source filled it, the total below is a floor and the
    # screen has to say so — a count that is quietly wrong is worse
    # than a slow search.
    capped = (len(contacts) >= CAP) or (len(leads) >= CAP)
    accounts = _accounts_by_id(
        [c.account_id or c.company_id for c in contacts]
        + [l.company_id for l in leads])
    rows += [_shape_contact(c, accounts) for c in contacts]
    rows += [_shape_lead(l, accounts) for l in leads]

    # The two filters that need the account and the fallbacks, so they
    # are applied after the row is assembled rather than in SQL.
    if f['country']:
        want = f['country'].lower()
        rows = [r for r in rows if want in (r['country'] or '').lower()]
    if f['vertical']:
        want = vertical_master.canonical(f['vertical']) or f['vertical']
        rows = [r for r in rows
                if (r['vertical'] or '').lower() == want.lower()]
    if f['industry']:
        want = f['industry'].lower()
        rows = [r for r in rows if want in (r['industry'] or '').lower()]

    # The portal's own address is not a person. Before the merge, so
    # a robot row cannot absorb a real one's phone and survive.
    rows = [r for r in rows if not _is_a_robot(r)]

    rows = _merge(rows)
    for row in rows:
        # One line, however many places the person turned up in: the
        # list is read across, not down, and four labels in a cell is
        # noise where "and 3 leads" is information.
        extra = row.get('also_leads') or 0
        first = (row.get('sources') or [row['label']])[0]
        row['source_note'] = (
            f'{first} · also on {extra} lead{"s" if extra > 1 else ""}'
            if extra else first)
    key = SORTS.get(sort, SORTS['name'])
    rows.sort(key=key, reverse=bool(descending))

    total = len(rows)
    page = max(int(page or 1), 1)
    per_page = max(min(int(per_page or PER_PAGE), 200), 1)
    start = (page - 1) * per_page
    return {
        'rows': rows[start:start + per_page],
        'total': total,
        'capped': capped,
        'capped_note': (f'More than {CAP} rows matched before the '
                        f'de-duplication, so this count is a floor. '
                        f'Narrow the search.') if capped else '',
        'page': page,
        'per_page': per_page,
        'pages': (total + per_page - 1) // per_page,
        'asked': asked,
        'sort': sort,
        'descending': bool(descending),
        'took_ms': int((time.time() - started) * 1000),
    }


#: Above this share of one country, the filter is reporting the
#: default rather than the person. 80% on the user's instruction of
#: 15 Nov: 664 of 667 is 99.6%, and nothing honest sits near 80 here.
DOMINANT_AT = 80.0

#: Spellings of one country. Only the ones this book carries; an
#: unfamiliar value is its own country, never folded into a guess.
COUNTRY_SYNONYMS = {
    'india': 'India', 'indian': 'India', 'bharat': 'India',
    'ind': 'India', 'in': 'India', 'india.': 'India',
    'uae': 'United Arab Emirates',
    'united arab emirates': 'United Arab Emirates',
    'u a e': 'United Arab Emirates', 'dubai': 'United Arab Emirates',
    'usa': 'United States', 'us': 'United States',
    'u s a': 'United States',
    'united states of america': 'United States',
    'united states': 'United States',
    'uk': 'United Kingdom', 'u k': 'United Kingdom',
    'great britain': 'United Kingdom', 'britain': 'United Kingdom',
    'united kingdom': 'United Kingdom',
    'ksa': 'Saudi Arabia', 'saudi': 'Saudi Arabia',
    'saudi arabia': 'Saudi Arabia',
}


def country_group(value):
    """The one spelling of a country, for counting.

    UAT of 15 Nov: 664 "India", 2 "INDIA" and 1 "Indian" were counted
    as three countries, so the page reported the top value as
    "Indian", one contact, 0.1%, and called the filter trustworthy.
    Three spellings of one word are one country.
    """
    text = ' '.join(str(value or '').strip().lower().split())
    if not text:
        return ''
    text = text.rstrip('.')
    if text in COUNTRY_SYNONYMS:
        return COUNTRY_SYNONYMS[text]
    return text.title()


def country_health(*, threshold=DOMINANT_AT):
    """Whether the country filter can be trusted today.

    `api_create_contact` has always written `d.get('country','India')`,
    so the column is full and says almost nothing. The screen prints
    what this returns beside the filter: a filter that looks
    authoritative and is not is worse than one marked unreliable.
    """
    from app import Contact, db

    rows = db.session.query(Contact.country, db.func.count(Contact.id)) \
        .group_by(Contact.country).all()
    grouped, spellings = {}, {}
    total = 0
    for value, n in rows:
        total += n
        key = country_group(value) or '(blank)'
        grouped[key] = grouped.get(key, 0) + n
        held = spellings.setdefault(key, {})
        held[(value or '').strip() or '(blank)'] = \
            held.get((value or '').strip() or '(blank)', 0) + n
    ranked = sorted(grouped.items(), key=lambda kv: (-kv[1], kv[0]))
    top = ranked[0] if ranked else ('', 0)
    percent = (100.0 * top[1] / total) if total else 0.0
    bad = bool(total) and percent >= threshold
    return {
        'total': total,
        'values': [{'value': value, 'count': n,
                    'percent': round(100.0 * n / total, 1) if total else 0,
                    'spellings': sorted(
                        ({'value': k, 'count': v}
                         for k, v in spellings.get(value, {}).items()),
                        key=lambda d: -d['count'])}
                   for value, n in ranked[:12]],
        'distinct': len(grouped),
        'spellings_seen': len(rows),
        'threshold': threshold,
        'dominant': {'value': top[0], 'count': top[1],
                     'percent': round(percent, 1)},
        'trustworthy': not bad,
        'note': ('Every contact carries a country because the create '
                 'form has always defaulted it. Until that is '
                 'corrected, a country filter answers about the '
                 'default, not about the person.') if bad else '',
    }


def suggest(field, q='', *, sc=None, limit=12):
    """Values already in the data, for a type-ahead.

    From the DATA, not from a hard-coded list: a filter that offers a
    value nothing carries produces an empty result and looks broken,
    and one that hides a value somebody uses is worse.
    """
    from app import Company, Contact, Lead, db
    from app.services import vertical_master

    text = (q or '').strip().lower()
    found = {}

    def offer(value):
        label = (value or '').strip()
        if not label:
            return
        if text and text not in label.lower():
            return
        key = label.lower()
        held, n = found.get(key, (label, 0))
        found[key] = (held, n + 1)

    #: Account names and aliases, for the company type-ahead only.
    #: An account somebody has FILED is worth more than a company
    #: name a lead happens to carry — and an alias is how this CRM
    #: says "Amns is ArcelorMittal Nippon Steel" (14 Nov).
    known, known_lower = {}, set()
    if field == 'company' and text:
        try:
            from app.services import account_match
            for account, _norms, _stricts, _own in account_match.active_index():
                for value in [account.name] + list(
                        getattr(account, 'aliases', None) or []):
                    label = str(value or '').strip()
                    if label:
                        known[label.lower()] = label
                        known_lower.add(label.lower())
        except Exception:                                 # noqa: BLE001
            known, known_lower = {}, set()

    def rank(item):
        """Exact first, then an account or one of its aliases, then
        the names that START with what was typed.

        UAT 15 Nov: typing "Am" offered BHEL above Amns, because BHEL
        is on more rows. UAT 17 Nov: an exact or alias match should
        come first — typing the whole of a short account name and
        finding it third is the same complaint one step further on.
        How common a value is only ever decides ties.
        """
        label, n = item
        lower = label.lower()
        if text and lower == text:
            where = 0
        elif lower in known_lower and text and text in lower:
            where = 1
        elif text and lower.startswith(text):
            where = 2
        elif text and f' {text}' in lower:
            where = 3
        else:
            where = 4
        return (where, -n, lower)

    if field == 'company':
        for (value,) in db.session.query(Company.name).limit(5000):
            offer(value)
        # And what each account is also CALLED. Typing the full name
        # of an account filed under its short one offered nothing at
        # all before this, which is the 14 Nov alias reading missing
        # from the one place somebody types a company name.
        for label in sorted(known.values()):
            offer(label)
        for (value,) in db.session.query(Contact.company).limit(5000):
            offer(value)
        for (value,) in db.session.query(Lead.company).limit(8000):
            offer(value)
    elif field == 'country':
        for model, column in ((Contact, 'country'), (Company, 'country'),
                              (Lead, 'country')):
            col = getattr(model, column, None)
            if col is None:
                continue
            for (value,) in db.session.query(col).distinct().limit(500):
                offer(value)
    elif field == 'location':
        for model, columns in ((Contact, ('city', 'state')),
                               (Company, ('city', 'state')),
                               (Lead, ('city', 'state'))):
            for column in columns:
                col = getattr(model, column, None)
                if col is None:
                    continue
                for (value,) in db.session.query(col).distinct().limit(800):
                    offer(value)
    elif field == 'vertical':
        for name in vertical_master.CANONICAL:
            offer(name)
    elif field == 'industry':
        for model, column in ((Contact, 'industry'), (Company, 'industry')):
            col = getattr(model, column, None)
            if col is None:
                continue
            for (value,) in db.session.query(col).distinct().limit(800):
                offer(value)
    rows = sorted(found.values(), key=rank)
    return [name for name, _n in rows[:limit]]


def duplicates_of(*, email='', phone='', name='', company='', sc=None,
                  limit=8):
    """People who look like this one, before a new one is created.

    Asked three ways, strongest first: an email is one person, a
    phone is very nearly one, and a name AT a company is a judgement
    the person creating the record can make in a second.

    The first two go straight at the columns rather than through
    `search`, because "is this address already on file" is a lookup
    and running a whole search to filter its page afterwards would
    both miss rows and cost more.
    """
    from app import Contact, Lead
    from sqlalchemy import or_
    from app.access import scope as sc_mod

    out, seen = [], set()

    def take(rows, why):
        for row in rows:
            key = (row['kind'], row['id'])
            if key in seen:
                continue
            seen.add(key)
            row = dict(row)
            row['why'] = why
            out.append(row)
            if len(out) >= limit:
                return

    want_email = _email_key(email)
    want_phone = _digits(phone)

    if want_email:
        contacts = (sc_mod.contacts(Contact.query, sc=sc)
                    .filter(Contact.email.ilike(want_email))
                    .limit(limit).all())
        leads = (sc_mod.leads(
            Lead.query.filter(Lead.is_archived.isnot(True)), sc=sc)
            .filter(Lead.email.ilike(want_email),
                    Lead.pic.isnot(None), Lead.pic != '')
            .order_by(Lead.id.desc()).limit(limit).all())
        accounts = _accounts_by_id(
            [c.account_id or c.company_id for c in contacts]
            + [l.company_id for l in leads])
        take([r for r in
              _merge([_shape_contact(c, accounts) for c in contacts]
                     + [_shape_lead(l, accounts) for l in leads])
              if not _is_a_robot(r)],
             'the same email address')

    if want_phone and len(out) < limit:
        # The last five digits narrow it in SQL; the full ten decide
        # it in Python, because "+91 98200 00000" and "9820000000" are
        # one number written two ways and LIKE cannot see that.
        tail = _like(want_phone[-5:])
        contacts = (sc_mod.contacts(Contact.query, sc=sc)
                    .filter(or_(Contact.mobile.like(tail),
                                Contact.phone.like(tail)))
                    .limit(200).all())
        leads = (sc_mod.leads(
            Lead.query.filter(Lead.is_archived.isnot(True)), sc=sc)
            .filter(or_(Lead.phone.like(tail), Lead.phone2.like(tail)),
                    Lead.pic.isnot(None), Lead.pic != '')
            .order_by(Lead.id.desc()).limit(200).all())
        accounts = _accounts_by_id(
            [c.account_id or c.company_id for c in contacts]
            + [l.company_id for l in leads])
        rows = [r for r in
                _merge([_shape_contact(c, accounts) for c in contacts]
                       + [_shape_lead(l, accounts) for l in leads])
                if not _is_a_robot(r)]
        take([r for r in rows
              if want_phone in [_digits(p) for p in r['phones']]],
             'the same phone number')

    if (name or '').strip() and len(out) < limit:
        found = search(name=name, company=company, sc=sc,
                       per_page=max(limit * 4, 20))
        take([r for r in found['rows'] if same_person_name(name, r['person'])],
             'the same name at the same company' if (company or '').strip()
             else 'the same name')
    return out[:limit]


def same_person_name(asked, found):
    """Whether these two names are the same PERSON, for the duplicate
    check only.

    UAT 17 Nov: "Test" was offered as a duplicate of "Integration
    Test". The search is deliberately a substring match — somebody
    looking for a person they half remember needs that — but a
    duplicate WARNING on a substring is a warning about a different
    person, and the one thing a false duplicate costs is the record
    not being created.

    So a name-only match has to be the whole name, or the first and
    last of it: "Rajesh Sharma" matches "Rajesh Kumar Sharma", and
    "Test" matches nothing but "Test". An email or a phone is still
    matched as it was, because those ARE the person.
    """
    from app.services import card_fields

    def parts(value):
        text = re.sub(r'[^a-z0-9 ]', ' ', str(value or '').lower())
        return [p for p in text.split() if p]

    want, have = parts(asked), parts(found)
    if not want or not have:
        return False
    if want == have:
        return True
    # First and last, with any middle names ignored on either side.
    try:
        a = card_fields.split_name(asked)
        b = card_fields.split_name(found)
    except Exception:                                     # noqa: BLE001
        return False
    first_a, last_a = (a['first'] or '').lower(), (a['last'] or '').lower()
    first_b, last_b = (b['first'] or '').lower(), (b['last'] or '').lower()
    if not (first_a and last_a and first_b and last_b):
        return False
    return first_a == first_b and last_a == last_b
