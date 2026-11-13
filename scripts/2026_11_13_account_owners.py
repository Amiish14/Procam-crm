"""Accounts carrying open leads with nobody named for their vertical.

CRM-FINAL-13NOV item 15. Fox Brasil (#14) has twelve leads and no
owner row for any vertical, which is the state CRM-WA-01b's exemption
exists to work around rather than the state anybody wants.

    .venv/bin/python scripts/2026_11_13_account_owners.py --check
    .venv/bin/python scripts/2026_11_13_account_owners.py --check \
          --csv data/account_owners_13NOV
    .venv/bin/python scripts/2026_11_13_account_owners.py \
          --apply --csv data/account_owners_13NOV-freight-forwarding.csv
    .venv/bin/python scripts/2026_11_13_account_owners.py \
          --undo --batch ACCTOWN-20261113-1

THE PROPOSAL, AND WHY IT IS THE MOST FREQUENT LEAD OWNER

An account with no owner for a vertical is not a question with no
answer: somebody is already working those leads. The person who owns
most of this account's open leads in that vertical is the person the
work has already been going to, and naming them is recording what is
happening rather than deciding something new.

It is still a proposal. The vertical head ticks it, because "who owns
this customer" is a relationship decision and the busiest person on
it this month is not always the right answer -- a lead reassigned
during somebody's leave would otherwise make the stand-in the owner
of the account.

WHAT IS COUNTED

Open leads only, through the lead's OWN vertical (`account_gate
.vertical_of`, which reads the lead and not the account's default --
CRM-MAIL-02 item 3). One row per (account, vertical) pair, because
that is what an owner row is: Fox Brasil may need one person for Air
and another for Sea.

Accounts that are not customers -- "Unknown", one holding our own
domain -- are left out: `account_match.not_a_customer` refuses to link
a lead to them, so naming an owner for one would be furniture.

TICK, THEN APPLY, THEN UNDO

    apply       y / yes / 1 / x in the `apply` column
    emp_code    the proposal, or type a different code over it

Every write goes through `app.services.assignment`, the one path that
writes an owner row, so the history and the notifications happen as
they do from the screen. The batch id is on every audit row and
`--undo --batch` removes exactly the rows that batch created,
leaving any row somebody has edited since.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import Company, Employee, Lead, app, db            # noqa: E402

ACTOR = 'CRM-FINAL-13NOV'
ACTION = 'account.vertical_owner_set'
BATCH_PREFIX = 'ACCTOWN'
YES = ('y', 'yes', '1', 'x', 'true', 'ok', 'tick')
FIELDS = ('apply', 'emp_code', 'account_id', 'account', 'vertical',
          'open_leads', 'proposed', 'proposed_name', 'why',
          'other_owners_on_this_account')
DEFAULT_CSV = os.path.join(_ROOT, 'data', 'account_owners_13NOV')


def _rule(n=72):
    print('─' * n)


def survey():
    """[(account, vertical, leads, proposal, why)] worth a decision."""
    from app.services import account_gate, account_match, sales_rules

    rows = (Lead.query
            .filter(Lead.is_archived.isnot(True),
                    Lead.junk_deleted.isnot(True),
                    Lead.merged_into_id.is_(None),
                    Lead.company_id.isnot(None),
                    Lead.stage.in_(sales_rules.open_stages()))
            .all())
    grouped = defaultdict(list)
    for lead in rows:
        vertical = account_gate.vertical_of(lead)
        if not vertical:
            continue
        grouped[(lead.company_id, vertical)].append(lead)

    out = []
    for (account_id, vertical), leads in sorted(
            grouped.items(), key=lambda kv: -len(kv[1])):
        account = db.session.get(Company, account_id)
        if account is None or not getattr(account, 'is_active', False):
            continue
        if account_match.not_a_customer(account):
            continue
        if account_gate.owner_for(account, vertical):
            continue                      # already named
        proposal, why = _propose(leads)
        out.append((account, vertical, leads, proposal, why))
    return out


def _propose(leads):
    """(emp_code, why) — the person already doing the work, or ('','')."""
    counted = Counter((lead.assigned_to or '').strip().upper()
                      for lead in leads
                      if (lead.assigned_to or '').strip())
    if not counted:
        return '', 'none of these leads has an owner either'
    code, n = counted.most_common(1)[0]
    person = Employee.query.filter_by(emp_code=code).first()
    if person is None:
        return '', f'{code} owns {n} of them and is not in the employee master'
    if not getattr(person, 'is_active', False):
        return '', (f'{code} owns {n} of them and is deactivated — '
                    f'somebody active has to be named')
    share = f'{n} of {len(leads)}'
    return code, f'owns {share} of the open leads in this vertical'


def _owners_elsewhere(account):
    try:
        from app.models.assignment import AccountVerticalOwner
        rows = AccountVerticalOwner.query.filter_by(
            account_id=account.id).all()
    except Exception:                                     # noqa: BLE001
        return ''
    return '; '.join(f'{r.vertical}={r.emp_code}' for r in rows)


def row_for(account, vertical, leads, proposal, why):
    person = (Employee.query.filter_by(emp_code=proposal).first()
              if proposal else None)
    return {
        'apply': '',
        'emp_code': proposal,
        'account_id': account.id,
        'account': (account.name or '')[:60],
        'vertical': vertical,
        'open_leads': len(leads),
        'proposed': proposal,
        'proposed_name': (getattr(person, 'name', '') or '')[:40],
        'why': why[:120],
        'other_owners_on_this_account': _owners_elsewhere(account)[:120],
    }


def report(found):
    print(f'\n══ {len(found)} (account, vertical) pair(s) with open '
          f'leads and nobody named ══')
    with_proposal = [r for r in found if r[3]]
    print(f'  {len(with_proposal)} have somebody already doing the work, '
          f'who is the proposal.')
    print(f'  {len(found) - len(with_proposal)} have nobody to propose — '
          f'the leads are unowned too, so a person has to choose.')

    print('\n  the biggest first:')
    for account, vertical, leads, proposal, why in found[:25]:
        print(f'    #{account.id:<6} {(account.name or "")[:28]:<28} '
              f'{vertical:<20} {len(leads):>3} lead(s)  '
              f'→ {proposal or "—":<12} {why[:46]}')
    if len(found) > 25:
        print(f'    … and {len(found) - 25} more')

    by_vertical = Counter(v for _a, v, _l, _p, _w in found)
    print('\n  by vertical, which is how the files are split:')
    for vertical, n in by_vertical.most_common():
        print(f'    {n:>5}  {vertical}')


def write_csvs(found, base):
    base = os.path.abspath(base)
    os.makedirs(os.path.dirname(base) or '.', exist_ok=True)
    groups = defaultdict(list)
    for account, vertical, leads, proposal, why in found:
        groups[vertical].append(
            row_for(account, vertical, leads, proposal, why))
    written = []
    for vertical, rows in sorted(groups.items()):
        slug = ''.join(c if c.isalnum() else '-'
                       for c in vertical.lower()).strip('-') or 'none'
        path = f'{base}-{slug}.csv'
        _write_one(path, rows)
        written.append((path, len(rows)))
    allrows = [row_for(a, v, l, p, w) for a, v, l, p, w in found]
    _write_one(f'{base}-all.csv', allrows)
    written.append((f'{base}-all.csv', len(allrows)))
    print('\n  files:')
    for path, n in written:
        print(f'    {n:>5}  {path}')
    print('  Tick `apply` with y, or type a different code over '
          '`emp_code`, then --apply --csv <file>.')
    return written


def _write_one(path, rows):
    buf = io.StringIO()
    out = csv.DictWriter(buf, fieldnames=list(FIELDS))
    out.writeheader()
    for row in rows:
        out.writerow({k: row.get(k, '') for k in FIELDS})
    with open(path, 'w', newline='') as fh:
        fh.write(buf.getvalue())


def read_ticks(path):
    with open(path, newline='') as fh:
        rows = list(csv.DictReader(fh))
    wanted, skipped = [], []
    for row in rows:
        account_id = (row.get('account_id') or '').strip()
        if not account_id.isdigit():
            continue
        if (row.get('apply') or '').strip().lower() not in YES:
            continue
        code = (row.get('emp_code') or '').strip().upper()
        vertical = (row.get('vertical') or '').strip()
        if not code:
            skipped.append((account_id, 'ticked with no emp_code'))
            continue
        if not vertical:
            skipped.append((account_id, 'ticked with no vertical'))
            continue
        wanted.append((int(account_id), vertical, code))
    return wanted, skipped


def plan(wanted):
    from app.services import account_gate

    doable, refused = [], []
    for account_id, vertical, code in wanted:
        account = db.session.get(Company, account_id)
        if account is None:
            refused.append((account_id, 'no such account'))
            continue
        person = Employee.query.filter_by(emp_code=code).first()
        if person is None:
            refused.append((account_id, f'no employee {code}'))
            continue
        if not getattr(person, 'is_active', False):
            refused.append((account_id, f'{code} is deactivated'))
            continue
        already = account_gate.owner_for(account, vertical)
        if already:
            refused.append((account_id,
                            f'{vertical} is already owned by {already} '
                            f'— somebody named one since the CSV'))
            continue
        doable.append((account, vertical, person))
    return doable, refused


def next_batch_id(now=None):
    from app.models.audit import AuditEvent

    now = now or datetime.utcnow()
    prefix = f'{BATCH_PREFIX}-{now.strftime("%Y%m%d")}-'
    try:
        rows = (AuditEvent.query.filter(AuditEvent.actor == ACTOR)
                .order_by(AuditEvent.id.desc()).limit(500).all())
    except Exception:                                     # noqa: BLE001
        rows = []
    used = set()
    for row in rows:
        payload = row.new_value if isinstance(row.new_value, dict) else {}
        batch = str(payload.get('batch') or '')
        if batch.startswith(prefix):
            used.add(batch)
    return f'{prefix}{len(used) + 1}'


def apply(doable, *, batch):
    from app.models.assignment import AccountVerticalOwner
    from app.services import audit

    made = 0
    try:
        for account, vertical, person in doable:
            row = AccountVerticalOwner(
                account_id=account.id, vertical=vertical,
                emp_code=person.emp_code, source='CRM-FINAL-13NOV')
            db.session.add(row)
            db.session.flush()
            audit.record(ACTION, 'company', account.id,
                         new={'vertical': vertical,
                              'emp_code': person.emp_code,
                              'row_id': row.id, 'batch': batch},
                         actor=ACTOR,
                         reason=(f'item 15, batch {batch}: named from the '
                                 f'open leads already being worked'))
            print(f'  {account.name} / {vertical} → {person.emp_code}')
            made += 1
        db.session.commit()
    except Exception as exc:                              # noqa: BLE001
        db.session.rollback()
        print(f'\n  !! ROLLED BACK: {type(exc).__name__}: {str(exc)[:200]}')
        raise SystemExit(1)
    print(f'\n{made} owner row(s) written. Batch {batch}.')
    print(f'Undo: --undo --batch {batch}')
    return made


def undo(batch):
    from app.models.assignment import AccountVerticalOwner
    from app.models.audit import AuditEvent
    from app.services import audit

    rows = (AuditEvent.query
            .filter(AuditEvent.action == ACTION,
                    AuditEvent.actor == ACTOR)
            .order_by(AuditEvent.id.asc()).all())
    mine = []
    for row in rows:
        payload = row.new_value if isinstance(row.new_value, dict) else {}
        if str(payload.get('batch') or '') == batch:
            mine.append(payload)
    if not mine:
        print(f'\nno batch {batch} on file.')
        return 0
    removed = 0
    try:
        for payload in mine:
            row = db.session.get(AccountVerticalOwner,
                                 payload.get('row_id') or 0)
            if row is None:
                continue
            if (row.emp_code or '').strip().upper() != \
                    str(payload.get('emp_code') or '').strip().upper():
                print(f'  owner row #{row.id} has been changed since the '
                      f'batch — left alone')
                continue
            db.session.delete(row)
            audit.record('account.vertical_owner_removed', 'company',
                         row.account_id,
                         old={'vertical': row.vertical,
                              'emp_code': row.emp_code},
                         new={'batch': batch},
                         actor=ACTOR, reason=f'undo of batch {batch}')
            removed += 1
        db.session.commit()
    except Exception as exc:                              # noqa: BLE001
        db.session.rollback()
        print(f'\n  !! ROLLED BACK: {type(exc).__name__}: {str(exc)[:200]}')
        raise SystemExit(1)
    print(f'\n{removed} owner row(s) removed.')
    return removed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--undo', action='store_true')
    ap.add_argument('--batch', default=None, metavar='ID')
    ap.add_argument('--csv', nargs='?', const=DEFAULT_CSV, default=None,
                    metavar='PATH')
    args = ap.parse_args(argv)

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('CRM-FINAL-13NOV item 15 — accounts with open leads and '
              'no owner.' + ('' if args.apply else ' REPORT ONLY.'))
        _rule()

        if args.undo:
            if not args.batch:
                print('\n--undo needs --batch ACCTOWN-YYYYMMDD-n')
                return 2
            undo(args.batch)
            return 0

        if args.apply:
            if not args.csv:
                print('\n--apply needs --csv <the ticked file>')
                return 2
            wanted, bad = read_ticks(args.csv)
            doable, refused = plan(wanted)
            print(f'\nticked: {len(wanted)}; applying {len(doable)}')
            for account_id, why in (bad + refused):
                print(f'  !! #{account_id}: {why}')
            apply(doable, batch=next_batch_id())
            return 0

        found = survey()
        report(found)
        if args.csv:
            write_csvs(found, args.csv)
        else:
            print('\nNo CSV written. --csv data/account_owners_13NOV '
                  'writes one file per vertical.')
        print('\nDRY RUN — nothing written.')
        db.session.rollback()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
