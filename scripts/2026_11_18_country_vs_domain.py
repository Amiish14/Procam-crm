"""Contacts filed as India whose own evidence says otherwise.
UAT 18 Nov item 8, extended 19 Nov items 3 and 4.

    # dry run, which is the default. Writes nothing.
    .venv/bin/python scripts/2026_11_18_country_vs_domain.py
    # the same, and the CSVs somewhere else
    ... --out docs/country_vs_domain.csv --phones-out docs/shared_phones.csv
    # write the corrections
    ... --apply --actor DIR12010
    # put a batch back
    ... --undo --batch CTRY-20261119-1 --actor DIR12010

TWO KINDS OF EVIDENCE, AND BOTH HAVE TO AGREE WITH EACH OTHER

  the dialling code   `+358…` is Finland, `+86…` China. Only a number
                      written in international form counts: a bare
                      ten-digit number is what every number in this
                      book looks like and says nothing.
  the email domain    the LAST label, where it is a country code.
                      `.fi` is Finland, `.de` Germany.

A row is only corrected where at least one of the two names a country,
neither of them names a DIFFERENT one, and the country on file is
India. Where the phone says Finland and the domain says Germany the row
is listed and NOT touched: two pieces of evidence that disagree are a
question for a person, and picking the louder one is how a bad backfill
looks like a good one.

WHY ONLY ROWS FILED AS INDIA

Because that is the fault being corrected: the Add Contact form used to
arrive with "India" in the box, so 670 contacts are Indian by default
rather than by anybody's decision. A contact filed as Finland whose
domain is `.de` is a different question -- somebody chose Finland --
and it is listed under `--all` for reading, never written.

WHAT IS DELIBERATELY NOT EVIDENCE

  * a generic top level -- com, net, org, io and the rest. A `.com`
    address says nothing about a country in either direction, which is
    why `hacklingroup.com` cannot be read as Finland however obvious it
    looks to a person who knows the company. Where the PHONE says
    Finland, the row is corrected on that.
  * a free mailbox (gmail, outlook, yahoo…). A personal address carries
    no country.
  * the city or the name. Guessing a country from either would be the
    same kind of guess as the one being undone.

ITEM 4 -- SHARED PHONES, REPORTED AND NEVER CHANGED

`+91-7738155449` sits on Raki Zhang and on Ville Ekholm at Hacklin:
two contacts at two companies on one number. That is either a switched
line, a shared desk, or one of them carrying somebody else's number,
and all three are decided by a person. So they go in their own CSV with
the number, the contacts and the companies, and a number belonging to a
Procam user is flagged -- ours on a customer contact is the worst of
the three and the easiest to miss.
"""
import argparse
import csv
import os
import re
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ACTION = 'contact.country_from_evidence'
UNDONE = 'contact.country_from_evidence_undone'
BATCH_PREFIX = 'CTRY'

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

GENERIC_TLD = {'com', 'net', 'org', 'biz', 'info', 'io', 'co', 'me',
               'app', 'dev', 'ai', 'cloud', 'global', 'group', 'online',
               'site', 'xyz', 'tech', 'world', 'company', 'email'}

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


def from_domain(domain):
    """(country, tld) the domain suggests, or (None, tld)."""
    if not domain or domain in FREE_MAIL:
        return None, ''
    bits = domain.split('.')
    if len(bits) < 2:
        return None, ''
    tld = bits[-1]
    if tld in GENERIC_TLD or tld == 'in':
        return None, tld
    return CC_TLD.get(tld), tld


def phones_of(row, *, drop_ours=True):
    """(numbers worth reading, numbers that are ours).

    UAT 19 Nov item 6. A number of OURS on a customer's record is not
    evidence about where that customer sits -- it is our own signature,
    captured off a forwarded enquiry. Counting it made Ville Ekholm a
    conflict (a .fi domain against an Indian mobile) when the mobile was
    never his: with it set aside, the domain is the only evidence and he
    is in Finland.

    `data/own_numbers.txt` is the same list the ingest reads, written by
    scripts/2026_11_19_shared_phone_evidence.py.
    """
    out, ours = [], []
    try:
        from app.services import own_numbers
        mine = own_numbers.is_ours
    except Exception:                                      # noqa: BLE001
        def mine(_v):
            return False
    for attr in ('phone', 'phone2', 'mobile', 'alt_phone'):
        said = getattr(row, attr, None)
        if not said:
            continue
        if drop_ours and mine(said):
            ours.append(str(said))
            continue
        out.append(str(said))
    return out, ours


def emails_of(row):
    """Every address on the record. A lead carries two."""
    out = []
    for attr in ('email', 'email2'):
        said = getattr(row, attr, None)
        if said and '@' in str(said):
            out.append(str(said))
    return out


def name_of(row):
    """The person's name, whichever table this row came from."""
    return (getattr(row, 'name', None)
            or getattr(row, 'pic', None) or '')


def evidence(row):
    """(country, how, detail) or (None, '', detail) for one record.

    Works on a contact or on a lead: both carry addresses and numbers,
    and `name_of` knows which column holds the person.

    Returns None where there is nothing, and also where the two kinds
    of evidence name two different countries -- a disagreement is not a
    suggestion.
    """
    from app.services.people_search import country_from_phone

    by_domain, tld, domain = None, '', ''
    for address in emails_of(row):
        said = domain_of(address)
        country, this_tld = from_domain(said)
        if country:
            by_domain, tld, domain = country, this_tld, said
            break
        domain = domain or said
    numbers, ours = phones_of(row)
    by_phone = country_from_phone(*numbers) or None
    said = {c for c in (by_domain, by_phone) if c}
    detail = ' / '.join(
        [f'domain {domain} (.{tld})' if by_domain else
         (f'domain {domain}' if domain else 'no email')]
        + [f'phone {by_phone}' if by_phone else 'no international phone']
        + ([f'{len(ours)} of our own number(s) set aside'] if ours else []))
    if len(said) > 1:
        return None, 'disagree', (
            f'domain says {by_domain}, phone says {by_phone} — left alone')
    if not said:
        return None, '', detail
    country = said.pop()
    how = ('the dialling code and the domain'
           if by_domain and by_phone else
           ('the dialling code' if by_phone else 'the email domain'))
    return country, how, detail


def _ours(number):
    """Is this number a Procam user's? Item 4's flag."""
    from app import Employee
    want = re.sub(r'\D', '', str(number or ''))[-10:]
    if len(want) < 10:
        return ''
    for attr in ('phone', 'mobile', 'contact_number'):
        if not hasattr(Employee, attr):
            continue
        for emp in Employee.query.all():
            said = re.sub(r'\D', '', str(getattr(emp, attr, '') or ''))
            if said and said[-10:] == want:
                return getattr(emp, 'name', None) or getattr(
                    emp, 'employee_id', '') or 'a Procam user'
        break
    return ''


def _sources():
    """(label, query) for every table that files a country on a person.

    UAT 19 Nov item 5: the lead PICs were missing. Mikko Lindroos is the
    person on lead #12168 on a `.fi` address and was filed as India --
    and no amount of reading the contacts table was ever going to find
    him, because he is not in it.
    """
    from app import Contact, Lead
    yield 'contact', Contact.query
    yield 'lead', (Lead.query
                   .filter(Lead.is_archived.isnot(True),
                           Lead.junk_deleted.isnot(True),
                           Lead.merged_into_id.is_(None),
                           Lead.pic.isnot(None), Lead.pic != ''))


def survey(every=False):
    rows, skipped, counted = [], defaultdict(int), 0
    for kind, query in _sources():
        for row in query.all():
            counted += 1
            filed = (row.country or '').strip()
            is_india = filed.lower() in INDIA
            country, how, detail = evidence(row)
            if how == 'disagree':
                skipped['the two kinds of evidence disagree'] += 1
                if not every:
                    continue
            if country is None and how != 'disagree':
                skipped['no evidence either way'] += 1
                continue
            if not every and not is_india:
                skipped['filed as something other than India'] += 1
                continue
            if country and filed.lower() == country.lower():
                skipped['already agrees'] += 1
                continue
            rows.append({
                'record': kind,
                'id': row.id,
                'name': name_of(row),
                'company': (getattr(row, 'company', '') or ''),
                'email': '; '.join(emails_of(row)),
                'phones': '; '.join(phones_of(row)[0]),
                'country_on_record': filed or '(blank)',
                'country_the_evidence_suggests': country or '(conflicting)',
                'evidence': how or 'conflicting',
                'detail': detail,
                'would_change': 'yes' if (country and is_india) else 'no',
                '_row': row,
                '_kind': kind,
                '_country': country,
            })
    rows.sort(key=lambda r: (r['country_the_evidence_suggests'],
                             r['record'], r['company'].lower(),
                             r['name'].lower()))
    return rows, skipped, counted


def shared_phones():
    """One number on contacts of two or more companies. Item 4."""
    from app import Contact

    by_number = defaultdict(list)
    for c in Contact.query.all():
        for number in phones_of(c, drop_ours=False)[0]:
            digits = re.sub(r'\D', '', number)[-10:]
            if len(digits) == 10:
                by_number[digits].append((c, number))
    out = []
    for digits, holders in by_number.items():
        companies = {(getattr(c, 'company', '') or '').strip().lower()
                     for c, _n in holders} - {''}
        if len(holders) < 2 or len(companies) < 2:
            continue
        whose = _ours(digits)
        out.append({
            'phone': digits,
            'as_written': '; '.join(sorted({n for _c, n in holders})),
            'contacts': '; '.join(sorted((c.name or f'#{c.id}')
                                         for c, _n in holders)),
            'contact_ids': '; '.join(str(c.id) for c, _n in holders),
            'companies': '; '.join(sorted(
                (getattr(c, 'company', '') or '—') for c, _n in holders)),
            'belongs_to_a_procam_user': whose or '',
        })
    out.sort(key=lambda r: (not r['belongs_to_a_procam_user'],
                            -len(r['contact_ids'].split(';')), r['phone']))
    return out


def write_csv(path, rows, fields):
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    return path


def next_batch_id(now=None):
    from app.models.audit import AuditEvent

    now = now or datetime.utcnow()
    prefix = f'{BATCH_PREFIX}-{now.strftime("%Y%m%d")}-'
    used = set()
    for r in (AuditEvent.query.filter(AuditEvent.action == ACTION)
              .order_by(AuditEvent.id.desc()).limit(2000).all()):
        said = str((r.new_value or {}).get('batch') or '') \
            if isinstance(r.new_value, dict) else ''
        if said.startswith(prefix):
            used.add(said)
    return f'{prefix}{len(used) + 1}'


def apply(rows, *, batch, actor):
    from app import db
    from app.services import audit

    n = 0
    try:
        for r in rows:
            if r['would_change'] != 'yes':
                continue
            row, want, kind = r['_row'], r['_country'], r['_kind']
            was = (row.country or '').strip()
            if was.lower() not in INDIA:
                continue        # edited since the survey
            row.country = want
            audit.record(ACTION, kind, row.id,
                         old={'country': was},
                         new={'country': want, 'batch': batch,
                              'record': kind},
                         actor=actor,
                         reason=(f'batch {batch}: {r["evidence"]} says '
                                 f'{want} — {r["detail"]}'),
                         commit=False)
            n += 1
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    print(f'\n{n} country value(s) corrected. Batch {batch}.')
    print(f'Undo: --undo --batch {batch} --actor {actor}')
    return n


def undo(batch, actor):
    from app import Contact, Lead, db
    from app.models.audit import AuditEvent
    from app.services import audit

    models = {'contact': Contact, 'lead': Lead}
    back, left = 0, 0
    try:
        for ev in (AuditEvent.query.filter(AuditEvent.action == ACTION)
                   .order_by(AuditEvent.id.asc()).all()):
            new = ev.new_value if isinstance(ev.new_value, dict) else {}
            if str(new.get('batch') or '') != batch:
                continue
            # `record` names the table. Older rows carry none, and were
            # all contacts -- the lead half did not exist then.
            model = models.get(str(new.get('record') or ev.entity_type
                                   or 'contact'))
            if model is None:
                continue
            row = db.session.get(model, int(ev.entity_id))
            if row is None:
                continue
            if (row.country or '').strip() != (new.get('country') or ''):
                left += 1       # somebody has edited it since; theirs stands
                continue
            was = (ev.old_value or {}).get('country') \
                if isinstance(ev.old_value, dict) else None
            row.country = was
            audit.record(UNDONE, ev.entity_type, row.id,
                         old={'country': new.get('country')},
                         new={'country': was, 'batch': batch,
                              'record': new.get('record') or 'contact'},
                         actor=actor,
                         reason=f'undo of batch {batch}', commit=False)
            back += 1
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    print(f'{back} row(s) put back.'
          + (f' {left} left alone — edited since the batch.' if left else ''))
    return back


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='docs/country_vs_domain.csv')
    ap.add_argument('--phones-out', dest='phones_out',
                    default='docs/shared_phones.csv')
    ap.add_argument('--all', action='store_true',
                    help='every disagreement, not only those filed as India')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--undo', action='store_true')
    ap.add_argument('--batch')
    ap.add_argument('--actor', help='employee id; required with --apply')
    a = ap.parse_args()

    os.environ.setdefault('SECRET_KEY', 'read-only')
    os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'read-only-0000000')
    from app import app as flask_app

    with flask_app.app_context():
        if a.undo:
            if not (a.batch and a.actor):
                sys.exit('  [STOP] --undo needs --batch and --actor.')
            undo(a.batch, a.actor)
            return
        if a.apply and not a.actor:
            sys.exit('  [STOP] --apply needs --actor, an employee id.')

        rows, skipped, counted = survey(every=a.all)
        would = [r for r in rows if r['would_change'] == 'yes']
        fields = ['record', 'id', 'name', 'company', 'email', 'phones',
                  'country_on_record', 'country_the_evidence_suggests',
                  'evidence', 'detail', 'would_change']
        path = write_csv(a.out, rows, fields)

        print(f'{counted} record(s) read — contacts and lead PICs.')
        for why, n in sorted(skipped.items(), key=lambda kv: -kv[1]):
            print(f'  {n:>6}  {why}')
        print(f'  {len(rows):>6}  listed, of which {len(would)} '
              f'would be corrected')
        by_country = defaultdict(int)
        for r in would:
            by_country[r['country_the_evidence_suggests']] += 1
        for country, n in sorted(by_country.items(), key=lambda kv: -kv[1]):
            print(f'      {n:>5}  India → {country}')
        print(f'\nCSV: {path}')

        # ── item 4, always printed, never applied ──
        shared = shared_phones()
        ppath = write_csv(a.phones_out, shared,
                          ['phone', 'as_written', 'contacts', 'contact_ids',
                           'companies', 'belongs_to_a_procam_user'])
        ours = [r for r in shared if r['belongs_to_a_procam_user']]
        print(f'\n{len(shared)} phone number(s) shared across companies'
              + (f', {len(ours)} of them a Procam user\'s' if ours else ''))
        for r in shared[:12]:
            flag = (f'  <-- {r["belongs_to_a_procam_user"]}'
                    if r['belongs_to_a_procam_user'] else '')
            print(f'  {r["phone"]}  {r["contacts"]}  '
                  f'[{r["companies"]}]{flag}')
        if len(shared) > 12:
            print(f'  … and {len(shared) - 12} more in the CSV')
        print(f'CSV: {ppath}')
        print('Shared phones are REPORTED ONLY. Which contact the number '
              'belongs to is\na question for a person, and --apply does not '
              'touch them.')

        if a.apply:
            batch = a.batch or next_batch_id()
            apply(rows, batch=batch, actor=a.actor)
        else:
            print(f'\nDry run. {len(would)} row(s) would change. '
                  f'Add --apply --actor DIR12010 to write.')


if __name__ == '__main__':
    main()
