"""Contacts filed as India whose own email domain says otherwise.
CRM UAT 18 Nov item 8.

READ ONLY. There is no --apply and there never will be in this file: it
writes a CSV and changes nothing. The country on a contact is a fact
about a person, and 670 of them say India because a form arrived with
"India" already in the box -- which is exactly why this list must be
read by somebody rather than corrected by a script that would be making
the same kind of guess in the opposite direction.

    .venv/bin/python scripts/2026_11_18_country_vs_domain.py
    .venv/bin/python scripts/2026_11_18_country_vs_domain.py \\
        --out /tmp/country_vs_domain.csv
    .venv/bin/python scripts/2026_11_18_country_vs_domain.py --all

WHAT COUNTS AS A DISAGREEMENT

The contact is filed under India, and the top level of their email
domain is a country code that is not India. `.fi` is Finland, `.de`
Germany, `.sg` Singapore. Nothing else is read: a company's own
`.com` address says nothing about where the person sits, and guessing
from a name or a city would be a worse guess than the one being
corrected.

WHAT IS DELIBERATELY NOT FLAGGED

  * `.in`, and India filed against it. That is agreement.
  * a generic top level -- com, net, org, biz, info, io, co -- whoever
    it is filed under. A .com address is not evidence of a country in
    either direction, and listing them would bury the real ones in
    noise.
  * a free mailbox (gmail, outlook, yahoo and the rest). Those are
    personal addresses and carry no country at all.
  * a contact with no address, which has no evidence either way.

The CSV is one row per contact with the evidence beside the figure, so
whoever reads it can see why each one is on the list.
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Country-code top levels worth naming. The country is what the
#: registry means by it, not where a company happens to trade.
CC_TLD = {
    'fi': 'Finland', 'de': 'Germany', 'sg': 'Singapore', 'cn': 'China',
    'ae': 'United Arab Emirates', 'nl': 'Netherlands', 'be': 'Belgium',
    'fr': 'France', 'it': 'Italy', 'es': 'Spain', 'pt': 'Portugal',
    'se': 'Sweden', 'no': 'Norway', 'dk': 'Denmark', 'pl': 'Poland',
    'cz': 'Czechia', 'at': 'Austria', 'ch': 'Switzerland',
    'uk': 'United Kingdom', 'gb': 'United Kingdom', 'ie': 'Ireland',
    'us': 'United States', 'ca': 'Canada', 'mx': 'Mexico',
    'br': 'Brazil', 'ar': 'Argentina', 'cl': 'Chile',
    'au': 'Australia', 'nz': 'New Zealand',
    'jp': 'Japan', 'kr': 'South Korea', 'tw': 'Taiwan', 'hk': 'Hong Kong',
    'my': 'Malaysia', 'th': 'Thailand', 'vn': 'Vietnam', 'id': 'Indonesia',
    'ph': 'Philippines', 'bd': 'Bangladesh', 'lk': 'Sri Lanka',
    'np': 'Nepal', 'pk': 'Pakistan',
    'sa': 'Saudi Arabia', 'qa': 'Qatar', 'om': 'Oman', 'kw': 'Kuwait',
    'bh': 'Bahrain', 'tr': 'Turkey', 'il': 'Israel', 'eg': 'Egypt',
    'za': 'South Africa', 'ke': 'Kenya', 'ng': 'Nigeria', 'tz': 'Tanzania',
    'ru': 'Russia', 'ua': 'Ukraine', 'kz': 'Kazakhstan',
    'gr': 'Greece', 'ro': 'Romania', 'hu': 'Hungary', 'bg': 'Bulgaria',
    'hr': 'Croatia', 'rs': 'Serbia', 'si': 'Slovenia', 'sk': 'Slovakia',
    'lt': 'Lithuania', 'lv': 'Latvia', 'ee': 'Estonia',
}

#: Says nothing about a country, in either direction.
GENERIC_TLD = {'com', 'net', 'org', 'biz', 'info', 'io', 'co', 'me',
               'app', 'dev', 'ai', 'cloud', 'global', 'group', 'online',
               'site', 'xyz', 'tech', 'world', 'company', 'email'}

#: A personal mailbox carries no country.
FREE_MAIL = {
    'gmail.com', 'googlemail.com', 'outlook.com', 'hotmail.com',
    'live.com', 'msn.com', 'yahoo.com', 'yahoo.co.in', 'ymail.com',
    'rediffmail.com', 'rediff.com', 'icloud.com', 'me.com', 'aol.com',
    'protonmail.com', 'proton.me', 'zoho.com', 'zohomail.com',
    'mail.com', 'gmx.com', 'yandex.com', 'qq.com', '163.com', '126.com',
}

INDIA = {'india', 'in', 'ind', 'bharat'}


def domain_of(address):
    said = (address or '').strip().lower()
    if '@' not in said:
        return ''
    return said.rsplit('@', 1)[1].strip().strip('.')


def suggests(domain):
    """(country, tld) the domain suggests, or (None, tld).

    Reads the LAST label. `co.uk` and `com.sg` both end in the country
    code, which is the label that carries the country -- the one in
    front of it is the registry's own category and is not a country.
    """
    if not domain or domain in FREE_MAIL:
        return None, ''
    bits = domain.split('.')
    if len(bits) < 2:
        return None, ''
    tld = bits[-1]
    if tld in GENERIC_TLD or tld == 'in':
        return None, tld
    return CC_TLD.get(tld), tld


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='docs/country_vs_domain.csv')
    ap.add_argument('--all', action='store_true',
                    help='every disagreement, not only those filed as India')
    a = ap.parse_args()

    os.environ.setdefault('SECRET_KEY', 'read-only')
    os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'read-only-0000000')
    from app import app, Contact

    with app.app_context():
        rows, counted, no_email, agreed, unknown_tld = [], 0, 0, 0, 0
        for c in Contact.query.all():
            counted += 1
            domain = domain_of(c.email)
            if not domain:
                no_email += 1
                continue
            filed = (c.country or '').strip()
            country, tld = suggests(domain)
            if country is None:
                if tld and tld not in GENERIC_TLD and tld != 'in':
                    unknown_tld += 1
                continue
            is_india = filed.lower() in INDIA
            if not a.all and not is_india:
                continue
            if filed.lower() == country.lower():
                agreed += 1
                continue
            rows.append({
                'contact_id': c.id,
                'name': c.name or '',
                'company': getattr(c, 'company', '') or '',
                'email_domain': domain,
                'tld': tld,
                'country_on_record': filed or '(blank)',
                'country_the_domain_suggests': country,
                'city': getattr(c, 'city', '') or '',
                'state': getattr(c, 'state', '') or '',
            })

        rows.sort(key=lambda r: (r['country_the_domain_suggests'],
                                 r['company'].lower(), r['name'].lower()))
        out = os.path.abspath(a.out)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, 'w', newline='', encoding='utf-8') as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows
                               else ['contact_id', 'name', 'company',
                                     'email_domain', 'tld',
                                     'country_on_record',
                                     'country_the_domain_suggests',
                                     'city', 'state'])
            w.writeheader()
            w.writerows(rows)

        by_country = {}
        for r in rows:
            key = r['country_the_domain_suggests']
            by_country[key] = by_country.get(key, 0) + 1

        print(f'{counted} contacts read.')
        print(f'  {no_email} have no email address — no evidence either way')
        print(f'  {agreed} agree with their domain')
        print(f'  {unknown_tld} end in a country code this script does not '
              f'know — add it to CC_TLD if it matters')
        print(f'  {len(rows)} disagree'
              + ('' if a.all else ' and are filed as India'))
        for country, n in sorted(by_country.items(), key=lambda kv: -kv[1]):
            print(f'      {n:>5}  {country}')
        print(f'\nCSV: {out}')
        print('Nothing was changed. Read it, decide, and the corrections '
              'go through\nthe usual dry-run/--apply/--undo script — not '
              'through this one.')


if __name__ == '__main__':
    main()
