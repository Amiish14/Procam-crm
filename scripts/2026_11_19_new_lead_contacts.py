"""Contacts filed under the company "New Lead", and the duplicates among them.
UAT 19 Nov item 8.

    # both surveys, read-only, two CSVs
    .venv/bin/python scripts/2026_11_19_new_lead_contacts.py
    # fill the company in from the evidence
    ... --apply --actor DIR12010
    # merge the pairs that differ only by the case of their email
    ... --merge --apply --actor DIR12010
    # put either back
    ... --undo --batch NEWLEAD-20261119-1 --actor DIR12010

TWO FAULTS, ONE FAMILY

Joachim Holst is in the book twice, #91 and #625, both at `fried-sped.de`
and both with the company "New Lead". That is the intake's placeholder
written into a real column: whatever created these contacts had no
company to hand and put the words "New Lead" there, and nothing has
asked since.

So there are two jobs, and they are deliberately separate commands:

  the default            every contact whose company is a placeholder,
                         with the company its own evidence suggests --
                         the linked account first, then the email
                         domain. Dry run; `--apply` fills it in.
  --merge                the pairs that are the SAME PERSON: identical
                         name and identical email but for case. The
                         older record is kept, the newer one's links are
                         moved onto it and it is DEACTIVATED, never
                         deleted.

WHAT COUNTS AS A DUPLICATE HERE, AND WHAT DOES NOT

Only `name` equal, ignoring case and spacing, AND `email` equal
ignoring case. That is the fault reported -- `Joachim.Holst@fried-sped.de`
against `joachim.holst@fried-sped.de` -- and nothing wider. Two people
who share a name are not merged, two addresses that differ by a letter
are not merged, and a contact with no email is never merged with
anybody: an empty address equals every other empty address, and that
would fold strangers together.

NOTHING IS EVER DELETED

`contacts.is_active` goes False on the record being folded away, its
`notes` gain a line naming the survivor, and every row that pointed at
it is repointed. The audit row holds the old and the new value of each
field it touched, so `--undo` puts the links back and reactivates it.

THE LINKS THAT MOVE are every table with a foreign key into `contacts`:

    account_activities.contact_id
    business_card_imports.linked_contact_id
    business_card_imports.created_contact_id
    contact_assignments.contact_id
    contact_relationships.contact_id
    project_contacts.contact_id

Read from the metadata at run time rather than listed here, so a table
added next month moves too instead of being silently left behind.
"""
import argparse
import csv
import os
import re
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FILL = 'contact.company_filled'
MERGE = 'contact.merged_into'
UNDONE_FILL = 'contact.company_filled_undone'
UNDONE_MERGE = 'contact.merged_into_undone'
BATCH_PREFIX = 'NEWLEAD'

#: Company values that are not a company. Compared on the squeezed,
#: lower-cased text, so "new lead", "New  Lead" and "NEW LEAD" are one.
PLACEHOLDERS = {
    'new lead', 'newlead', 'unknown', 'n/a', 'na', 'none', 'nil', '-',
    '--', 'tbd', 'tba', 'test', 'xxx', 'company', 'customer', 'client',
    'lead', 'enquiry', 'not known', 'not available', 'no company',
}

FREE_MAIL = {
    'gmail.com', 'googlemail.com', 'outlook.com', 'hotmail.com',
    'live.com', 'msn.com', 'yahoo.com', 'yahoo.co.in', 'ymail.com',
    'rediffmail.com', 'rediff.com', 'icloud.com', 'me.com', 'aol.com',
    'protonmail.com', 'proton.me', 'zoho.com', 'zohomail.com',
    'mail.com', 'gmx.com', 'yandex.com', 'qq.com', '163.com', '126.com',
}


def squeeze(value):
    return ' '.join(str(value or '').split()).strip().lower()


def is_placeholder(company):
    return squeeze(company) in PLACEHOLDERS


def domain_of(address):
    said = str(address or '').strip().lower()
    return said.rsplit('@', 1)[1].strip().strip('>,; ') \
        if '@' in said else ''


def company_from_domain(domain):
    """A readable company name out of a domain, or ''.

    `fried-sped.de` -> "Fried Sped". A guess, and labelled as one in the
    CSV: it is a starting point for whoever reads the list, not a fact.
    """
    if not domain or domain in FREE_MAIL:
        return ''
    bits = domain.split('.')
    root = bits[-3] if (len(bits) >= 3 and bits[-2] in
                        ('co', 'com', 'net', 'org', 'gov')) else bits[0]
    words = [w for w in re.split(r'[^a-z0-9]+', root.lower()) if w]
    return ' '.join(w.capitalize() for w in words)


def suggestion(contact, accounts):
    """(company, where it came from) for one placeholder contact."""
    account = accounts.get(contact.account_id or contact.company_id)
    if account is not None:
        said = (getattr(account, 'name', None) or '').strip()
        if said and not is_placeholder(said):
            return said, 'the linked account'
    domain = domain_of(contact.email)
    said = company_from_domain(domain)
    if said:
        return said, f'the email domain {domain}'
    if domain:
        return '', f'{domain} is a free mailbox — no company in it'
    return '', 'no account and no email'


def _accounts():
    # `Company` is the account model -- `people_search._accounts_by_id`
    # resolves `contacts.account_id or contacts.company_id` against it,
    # and this reads the same table so the two agree about what the
    # linked account IS.
    from app import Company
    return {a.id: a for a in Company.query.all()}


def survey():
    from app import Contact

    accounts = _accounts()
    rows = []
    for c in Contact.query.all():
        if not is_placeholder(c.company):
            continue
        said, why = suggestion(c, accounts)
        rows.append({
            'contact_id': c.id,
            'name': c.name or '',
            'email': c.email or '',
            'company_on_record': (c.company or '').strip(),
            'suggested_company': said,
            'suggested_from': why,
            'would_change': 'yes' if said else 'no',
            'created': str(getattr(c, 'created_at', '') or '')[:10],
            '_row': c,
            '_company': said,
        })
    rows.sort(key=lambda r: (r['would_change'] != 'yes',
                             r['suggested_company'].lower(),
                             r['name'].lower()))
    return rows


def duplicate_pairs(placeholder_only=False):
    """[(keep, fold)] — the same person twice, differing only by case.

    The OLDER record is kept: it is the one other rows have had longest
    to point at, and its id is the one people have seen.
    """
    from app import Contact

    by_key = defaultdict(list)
    for c in Contact.query.all():
        if placeholder_only and not is_placeholder(c.company):
            continue
        if getattr(c, 'is_active', True) is False:
            continue
        name, email = squeeze(c.name), squeeze(c.email)
        if not name or not email or '@' not in email:
            continue        # no address is not a match, it is a blank
        by_key[(name, email)].append(c)
    out = []
    for (_n, _e), group in by_key.items():
        if len(group) < 2:
            continue
        # Oldest first: by created_at where there is one, then by id,
        # which is the order they were written in.
        group.sort(key=lambda c: (getattr(c, 'created_at', None)
                                  or datetime.min, c.id))
        keep = group[0]
        for fold in group[1:]:
            # Identical already, including case? Then it is not the
            # fault reported -- but it is still the same person twice,
            # so it is still merged. Said in the CSV either way.
            out.append((keep, fold))
    out.sort(key=lambda pair: pair[0].id)
    return out


def _link_tables():
    """[(table, column)] for every foreign key into `contacts`."""
    from app import db
    out = []
    for table in db.metadata.sorted_tables:
        for fk in table.foreign_keys:
            if fk.column.table.name == 'contacts':
                out.append((table.name, fk.parent.name))
    return sorted(set(out))


def _link_counts(contact_id):
    from app import db
    out = {}
    for table, column in _link_tables():
        try:
            n = db.session.execute(db.text(
                f'SELECT COUNT(*) FROM {table} WHERE {column} = :i'),
                {'i': contact_id}).scalar()
        except Exception:                                  # noqa: BLE001
            n = None
        if n:
            out[f'{table}.{column}'] = n
    return out


def next_batch_id(now=None):
    from app.models.audit import AuditEvent
    now = now or datetime.utcnow()
    prefix = f'{BATCH_PREFIX}-{now.strftime("%Y%m%d")}-'
    used = set()
    for action in (FILL, MERGE):
        for r in (AuditEvent.query.filter(AuditEvent.action == action)
                  .order_by(AuditEvent.id.desc()).limit(2000).all()):
            said = str((r.new_value or {}).get('batch') or '') \
                if isinstance(r.new_value, dict) else ''
            if said.startswith(prefix):
                used.add(said)
    return f'{prefix}{len(used) + 1}'


def apply_fill(rows, *, batch, actor):
    from app import db
    from app.services import audit

    n = 0
    try:
        for r in rows:
            if r['would_change'] != 'yes':
                continue
            row, want = r['_row'], r['_company']
            was = (row.company or '').strip()
            if not is_placeholder(was):
                continue        # edited since the survey
            row.company = want
            audit.record(FILL, 'contact', row.id,
                         old={'company': was},
                         new={'company': want, 'batch': batch},
                         actor=actor,
                         reason=(f'batch {batch}: company from '
                                 f'{r["suggested_from"]}'),
                         commit=False)
            n += 1
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    print(f'\n{n} company value(s) filled in. Batch {batch}.')
    print(f'Undo: --undo --batch {batch} --actor {actor}')
    return n


def apply_merge(pairs, *, batch, actor):
    from app import db
    from app.services import audit

    n, moved_total = 0, 0
    try:
        for keep, fold in pairs:
            if getattr(fold, 'is_active', True) is False:
                continue        # already folded away
            moved = {}
            for table, column in _link_tables():
                try:
                    res = db.session.execute(db.text(
                        f'UPDATE {table} SET {column} = :keep '
                        f'WHERE {column} = :fold'),
                        {'keep': keep.id, 'fold': fold.id})
                    if res.rowcount:
                        moved[f'{table}.{column}'] = res.rowcount
                        moved_total += res.rowcount
                except Exception as exc:                   # noqa: BLE001
                    print(f'  [note] {table}.{column} not moved: {exc}')
            # What only the folded record knew is worth keeping.
            filled = {}
            for field in ('designation', 'phone', 'mobile', 'country',
                          'state', 'city', 'website', 'linkedin',
                          'department', 'address', 'postal_code',
                          'industry'):
                if not (getattr(keep, field, None) or '').strip() \
                        and (getattr(fold, field, None) or '').strip():
                    filled[field] = getattr(fold, field)
                    setattr(keep, field, getattr(fold, field))
            note = (f'Merged from contact #{fold.id} '
                    f'({fold.email}) — batch {batch}.')
            keep.notes = ((keep.notes or '') + ('\n' if keep.notes else '')
                          + note)
            was_active = getattr(fold, 'is_active', True)
            fold.is_active = False
            fold.notes = ((fold.notes or '') + ('\n' if fold.notes else '')
                          + f'Merged into contact #{keep.id} — '
                            f'batch {batch}. Not deleted.')
            audit.record(MERGE, 'contact', fold.id,
                         old={'is_active': was_active,
                              'links': moved, 'into': None},
                         new={'is_active': False, 'into': keep.id,
                              'links': moved, 'filled': filled,
                              'batch': batch},
                         actor=actor,
                         reason=(f'batch {batch}: #{fold.id} is #{keep.id} '
                                 f'again, differing only by the case of '
                                 f'the email'),
                         commit=False)
            n += 1
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    print(f'\n{n} duplicate(s) folded away, {moved_total} link(s) moved. '
          f'Batch {batch}.')
    print('Nothing was deleted: each one is deactivated and says which '
          'record it became.')
    print(f'Undo: --undo --batch {batch} --actor {actor}')
    return n


def undo(batch, actor):
    from app import Contact, db
    from app.models.audit import AuditEvent
    from app.services import audit

    back = fills = left = 0
    try:
        for ev in (AuditEvent.query.filter(
                AuditEvent.action.in_([FILL, MERGE]))
                .order_by(AuditEvent.id.desc()).all()):
            new = ev.new_value if isinstance(ev.new_value, dict) else {}
            old = ev.old_value if isinstance(ev.old_value, dict) else {}
            if str(new.get('batch') or '') != batch:
                continue
            row = db.session.get(Contact, int(ev.entity_id))
            if row is None:
                continue
            if ev.action == FILL:
                if (row.company or '').strip() != (new.get('company') or ''):
                    left += 1
                    continue
                row.company = old.get('company')
                audit.record(UNDONE_FILL, 'contact', row.id,
                             old={'company': new.get('company')},
                             new={'company': old.get('company'),
                                  'batch': batch},
                             actor=actor, reason=f'undo of batch {batch}',
                             commit=False)
                fills += 1
                continue
            # a merge: put the links back, reactivate, undo the fills
            keep_id = new.get('into')
            for key, n in (new.get('links') or {}).items():
                table, column = key.split('.', 1)
                try:
                    db.session.execute(db.text(
                        f'UPDATE {table} SET {column} = :fold '
                        f'WHERE {column} = :keep'),
                        {'fold': row.id, 'keep': keep_id})
                except Exception as exc:                   # noqa: BLE001
                    print(f'  [note] {key} not put back: {exc}')
            keep = db.session.get(Contact, int(keep_id)) if keep_id else None
            if keep is not None:
                for field, value in (new.get('filled') or {}).items():
                    if (getattr(keep, field, None) or '') == (value or ''):
                        setattr(keep, field, None)
            row.is_active = old.get('is_active', True)
            audit.record(UNDONE_MERGE, 'contact', row.id,
                         old={'is_active': False, 'into': keep_id},
                         new={'is_active': True, 'into': None,
                              'batch': batch},
                         actor=actor, reason=f'undo of batch {batch}',
                         commit=False)
            back += 1
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    print(f'{fills} company value(s) put back, {back} merge(s) reversed.'
          + (f' {left} left alone — edited since the batch.'
             if left else ''))
    return back + fills


def write_csv(path, rows, fields):
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='docs/new_lead_contacts.csv')
    ap.add_argument('--dupes-out', dest='dupes_out',
                    default='docs/new_lead_duplicates.csv')
    ap.add_argument('--merge', action='store_true',
                    help='the duplicate merge rather than the company fill')
    ap.add_argument('--all-dupes', dest='all_dupes', action='store_true',
                    help='case-duplicates anywhere, not only placeholders')
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

        rows = survey()
        would = [r for r in rows if r['would_change'] == 'yes']
        path = write_csv(a.out, rows,
                         ['contact_id', 'name', 'email',
                          'company_on_record', 'suggested_company',
                          'suggested_from', 'would_change', 'created'])

        pairs = duplicate_pairs(placeholder_only=not a.all_dupes)
        dupe_rows = []
        for keep, fold in pairs:
            dupe_rows.append({
                'keep_id': keep.id,
                'keep_email': keep.email or '',
                'keep_created': str(getattr(keep, 'created_at', '') or '')[:10],
                'fold_id': fold.id,
                'fold_email': fold.email or '',
                'fold_created': str(getattr(fold, 'created_at', '') or '')[:10],
                'name': keep.name or '',
                'company': (keep.company or '').strip(),
                'differs_by': ('the case of the email'
                               if (keep.email or '') != (fold.email or '')
                               else 'nothing — identical'),
                'links_to_move': '; '.join(
                    f'{k}={v}' for k, v in _link_counts(fold.id).items())
                                 or 'none',
            })
        dpath = write_csv(a.dupes_out, dupe_rows,
                          ['keep_id', 'keep_email', 'keep_created',
                           'fold_id', 'fold_email', 'fold_created',
                           'name', 'company', 'differs_by',
                           'links_to_move'])

        print(f'{len(rows)} contact(s) filed under a placeholder company.')
        print(f'  {len(would)} have a company in their own evidence')
        print(f'  {len(rows) - len(would)} do not — a free mailbox, or '
              f'nothing to read')
        by_source = defaultdict(int)
        for r in would:
            by_source[r['suggested_from'].split(' ')[1]
                      if r['suggested_from'].startswith('the email')
                      else r['suggested_from']] += 1
        for said, n in sorted(by_source.items(), key=lambda kv: -kv[1]):
            print(f'      {n:>5}  from {said}')
        for r in would[:15]:
            print(f'  #{r["contact_id"]:<6} {r["name"][:22]:<22} '
                  f'{r["company_on_record"][:10]:<10} → '
                  f'{r["suggested_company"][:24]:<24} '
                  f'({r["suggested_from"]})')
        if len(would) > 15:
            print(f'  … and {len(would) - 15} more in the CSV')
        print(f'CSV: {path}')

        print(f'\n{len(pairs)} duplicate pair(s): same name, same email '
              f'but for case'
              + ('' if a.all_dupes else ', among the placeholders'))
        for r in dupe_rows[:15]:
            print(f'  keep #{r["keep_id"]:<6} fold #{r["fold_id"]:<6} '
                  f'{r["name"][:22]:<22} {r["differs_by"]}'
                  f'  [{r["links_to_move"]}]')
        if len(dupe_rows) > 15:
            print(f'  … and {len(dupe_rows) - 15} more in the CSV')
        print(f'CSV: {dpath}')

        if a.apply and a.merge:
            apply_merge(pairs, batch=(a.batch or next_batch_id()),
                        actor=a.actor)
        elif a.apply:
            apply_fill(rows, batch=(a.batch or next_batch_id()),
                       actor=a.actor)
        else:
            print(f'\nDry run. --apply would fill {len(would)} company '
                  f'value(s);\n--merge --apply would fold {len(pairs)} '
                  f'duplicate(s) away, keeping the older record.\n'
                  f'Nothing is ever deleted.')


if __name__ == '__main__':
    main()
