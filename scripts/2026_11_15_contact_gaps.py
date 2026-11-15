"""What the contacts are missing, and the two gaps that can be filled.

CRM-SEARCH-SCAN-01 Part 1, the read-only half plus a sweep.

    .venv/bin/python scripts/2026_11_15_contact_gaps.py --check
    .venv/bin/python scripts/2026_11_15_contact_gaps.py --check --csv
    .venv/bin/python scripts/2026_11_15_contact_gaps.py --apply --actor DIR12010
    .venv/bin/python scripts/2026_11_15_contact_gaps.py --undo --batch CGAP-20261115-1

WHAT IT FILLS, AND FROM WHAT

  * **country, from the dialling code** — and only from a number
    written in international form. A ten-digit Indian mobile with no
    "+" says nothing: it is what every number in this book looks
    like, and reading India into it would make the country filter
    agree with itself, which is the fault being fixed rather than a
    fix for it.
  * **industry, from the linked account** — the account knows what
    the customer does; the contact row usually does not.

WHAT IT WILL NOT FILL

**Country where one is already stored.** Every contact carries one
because `api_create_contact` has always defaulted it to "India", so
the column is 100% full and largely meaningless. Overwriting a stored
value on that reasoning would be the sweep deciding which of them are
real, and it cannot know. The report counts them; correcting them is
a person's job, and the search screen says the filter is unreliable
until it is done.

There is no vertical on a contact. The desk is a property of the
enquiry and the account, and the search reads it from there.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from collections import Counter
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import app, db                                    # noqa: E402

ACTOR = 'CRM-SEARCH-SCAN-01'
ACTION = 'contact.gap_filled'
UNDONE = 'contact.gap_undone'
BATCH_PREFIX = 'CGAP'
DEFAULT_CSV = os.path.join(_ROOT, 'data', 'contact_gaps_15NOV.csv')


def _rule(n=76):
    print('─' * n)


def _blank(value):
    return not str(value or '').strip()


def survey():
    """[(contact, {field: (value, where from)})] for what can be filled."""
    from app import Company, Contact
    from app.services import people_search as ps

    accounts = {a.id: a for a in Company.query.all()}
    out = []
    for row in Contact.query.order_by(Contact.id.asc()).all():
        account = accounts.get(row.account_id or row.company_id or 0)
        fills = {}
        if _blank(row.country):
            country = ps.country_from_phone(row.mobile, row.phone)
            if country:
                fills['country'] = (country, 'the dialling code')
            elif account is not None and not _blank(account.country):
                fills['country'] = (account.country.strip(),
                                    f'account #{account.id}')
        if _blank(row.industry) and account is not None \
                and not _blank(account.industry):
            fills['industry'] = (account.industry.strip(),
                                 f'account #{account.id}')
        if _blank(row.city) and account is not None \
                and not _blank(account.city):
            fills['city'] = (account.city.strip(), f'account #{account.id}')
        if _blank(row.state) and account is not None \
                and not _blank(account.state):
            fills['state'] = (account.state.strip(),
                              f'account #{account.id}')
        if fills:
            out.append((row, fills))
    return out


def report(found):
    from app import Contact

    total = Contact.query.count()
    print(f'\n══ {total} contact(s) ══')
    blanks = Counter()
    for column in ('country', 'city', 'state', 'industry'):
        col = getattr(Contact, column, None)
        if col is None:
            continue
        blanks[column] = (Contact.query
                          .filter(db.or_(col.is_(None),
                                         db.func.trim(col) == ''))
                          .count())
    for column, n in blanks.items():
        pct = (100.0 * n / total) if total else 0
        print(f'  {n:>7} ({pct:5.1f}%) have no {column}')

    # The country column, and whether it means anything.
    rows = db.session.query(Contact.country, db.func.count(Contact.id)) \
        .group_by(Contact.country).all()
    if rows:
        print('\n  the country values on file:')
        for value, n in sorted(rows, key=lambda kv: -kv[1])[:12]:
            pct = (100.0 * n / total) if total else 0
            print(f'    {n:>7} ({pct:5.1f}%)  {value or "(blank)"}')
        top = max(rows, key=lambda kv: kv[1])
        if total and (100.0 * top[1] / total) >= 90.0:
            print(f'\n  !! {100.0 * top[1] / total:.1f}% are '
                  f'"{top[0]}". The new-contact form has always '
                  f'defaulted country')
            print('     to "India", so this column is full and says '
                  'almost nothing. The')
            print('     search screen marks the country filter '
                  'unreliable until that is')
            print('     corrected, and this sweep will not overwrite a '
                  'stored value.')

    print(f'\n══ {len(found)} contact(s) have a gap this can fill ══')
    by_field = Counter()
    for _row, fills in found:
        for field in fills:
            by_field[field] += 1
    for field, n in by_field.most_common():
        print(f'  {n:>7}  {field}')
    for row, fills in found[:25]:
        what = ', '.join(f'{k}={v!r} from {how}'
                         for k, (v, how) in sorted(fills.items()))
        print(f'    #{row.id} {(row.name or "")[:26]:<26} {what}')
    if len(found) > 25:
        print(f'    … and {len(found) - 25} more')


def write_csv(found, path):
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(['contact_id', 'name', 'field', 'value', 'from'])
    for row, fills in found:
        for field, (value, how) in sorted(fills.items()):
            out.writerow([row.id, row.name or '', field, value, how])
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='') as fh:
        fh.write(buf.getvalue())
    print(f'\nCSV: {path}')


def next_batch_id(now=None):
    from app.models.audit import AuditEvent

    now = now or datetime.utcnow()
    prefix = f'{BATCH_PREFIX}-{now.strftime("%Y%m%d")}-'
    try:
        rows = (AuditEvent.query.filter(AuditEvent.action == ACTION)
                .order_by(AuditEvent.id.desc()).limit(500).all())
    except Exception:                                     # noqa: BLE001
        rows = []
    used = {str((r.new_value or {}).get('batch') or '')
            for r in rows if isinstance(r.new_value, dict)}
    used = {b for b in used if b.startswith(prefix)}
    return f'{prefix}{len(used) + 1}'


def apply(found, *, batch, actor):
    from app.services import audit

    done = Counter()
    try:
        for row, fills in found:
            wrote = {}
            for field, (value, how) in fills.items():
                if not _blank(getattr(row, field, None)):
                    continue          # somebody filled it since
                setattr(row, field, value)
                wrote[field] = {'value': value, 'from': how}
                done[field] += 1
            if not wrote:
                continue
            audit.record(ACTION, 'contact', row.id,
                         old={k: None for k in wrote},
                         new={'filled': wrote, 'batch': batch},
                         actor=actor,
                         reason=(f'batch {batch}: filled from the '
                                 f'dialling code and the linked '
                                 f'account, so the search can find '
                                 f'this person by them'),
                         commit=False)
            done['contacts touched'] += 1
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    print()
    for what, n in done.most_common():
        print(f'  {n:>7}  {what}')
    print(f'\nBatch {batch}. Undo: --undo --batch {batch}')
    return done


def undo(batch):
    from app import Contact
    from app.models.audit import AuditEvent
    from app.services import audit

    back, left = 0, 0
    for row in (AuditEvent.query.filter(AuditEvent.action == ACTION)
                .order_by(AuditEvent.id.asc()).all()):
        payload = row.new_value if isinstance(row.new_value, dict) else {}
        if str(payload.get('batch') or '') != batch:
            continue
        contact = db.session.get(Contact, int(row.entity_id))
        if contact is None:
            continue
        filled = payload.get('filled') or {}
        changed = False
        for field, what in filled.items():
            if (getattr(contact, field, None) or '') != (what.get('value') or ''):
                left += 1
                continue
            setattr(contact, field, None)
            changed = True
        if changed:
            audit.record(UNDONE, 'contact', contact.id,
                         old={'filled': filled},
                         new={'batch': batch}, actor=ACTOR,
                         reason=f'undo of batch {batch}', commit=False)
            back += 1
    db.session.commit()
    print(f'\n{back} contact(s) emptied again; {left} field(s) had '
          f'changed since the batch and were left.')
    return back


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--undo', action='store_true')
    ap.add_argument('--batch', default=None, metavar='ID')
    ap.add_argument('--actor', default=None, metavar='EMP_CODE')
    ap.add_argument('--csv', nargs='?', const=DEFAULT_CSV, default=None,
                    metavar='PATH')
    args = ap.parse_args(argv)

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('contact gaps.'
              + ('' if (args.apply or args.undo) else ' REPORT ONLY.'))
        _rule()
        if args.undo:
            if not args.batch:
                print('\n--undo needs --batch CGAP-YYYYMMDD-n')
                return 2
            undo(args.batch)
            return 0

        found = survey()
        report(found)
        if args.csv:
            write_csv(found, args.csv)
        if not args.apply:
            print(f'\nDRY RUN — nothing written. --apply --actor '
                  f'<EMP_CODE> fills the {len(found)} contact(s) '
                  f'above.')
            db.session.rollback()
            return 0
        if not args.actor:
            print('\n--apply needs --actor <EMP_CODE>.')
            return 2
        apply(found, batch=next_batch_id(),
              actor=args.actor.strip().upper())
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
