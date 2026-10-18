"""
Who serves each account, for each kind of work — from its history.

READ-ONLY unless `--apply`.

    .venv/bin/python scripts/seed_account_vertical_owners.py --check
    .venv/bin/python scripts/seed_account_vertical_owners.py --apply

For every account, and every vertical it has had work in, the owner
of its most recent open or won lead in that vertical. That is the
best evidence the CRM holds about who actually serves whom, and it
is evidence rather than a decision: the row is marked `seeded`, so a
person can tell it from one somebody set deliberately.

Skipped, and reported rather than guessed:

    an owner who has left, unless the replacement map covers them
    a vertical that is an alias — the official name is used instead
    an account whose only leads are lost
"""
import argparse
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Company, Employee, Lead, app, db          # noqa: E402

#: Stages whose owner is worth copying. A lost lead's owner is not
#: evidence of who serves the account now.
GOOD_STAGES_EXTRA = ('Won',)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    if args.apply == args.check:
        raise SystemExit('Choose one: --check or --apply.')

    with app.app_context():
        from app.models.assignment import AccountVerticalOwner
        from app.services import auto_assign, sales_rules as rules
        from app.services import vertical_heads as vh

        print(f'database: {db.engine.url}')
        print(f'MODE:     {"APPLY" if args.apply else "DRY RUN — nothing is written"}\n')

        wanted = set(rules.open_stages()) | set(GOOD_STAGES_EXTRA)
        active = {e.emp_code for e in
                  Employee.query.filter(Employee.is_active.is_(True)).all()}

        # newest first, so the first one seen per (account, vertical)
        # wins without sorting per group
        leads = (Lead.query
                 .filter(Lead.company_id.isnot(None),
                         Lead.is_archived.isnot(True),
                         Lead.assigned_to.isnot(None),
                         Lead.stage.in_(tuple(wanted)))
                 .order_by(Lead.updated_at.desc()).all())

        best, skipped = {}, Counter()
        for l in leads:
            vertical = vh.master_vertical(l.procam_vertical or '')
            if not vertical or not (l.assigned_to or '').strip():
                continue
            key = (l.company_id, vertical)
            if key in best:
                continue
            owner = l.assigned_to.strip().upper()
            if owner not in active:
                replacement = auto_assign.replacement_for(owner)
                if not replacement:
                    skipped['owner has left, no replacement'] += 1
                    continue
                owner = replacement
                skipped['owner has left, replacement used'] += 1
            best[key] = {'account_id': l.company_id, 'vertical': vertical,
                         'emp_code': owner, 'from_lead': l.id,
                         'stage': l.stage}

        existing = {(r.account_id, r.vertical): r
                    for r in AccountVerticalOwner.query.all()}
        new = [v for k, v in best.items() if k not in existing]
        same = [v for k, v in best.items()
                if k in existing and existing[k].emp_code == v['emp_code']]
        differ = [(v, existing[k]) for k, v in best.items()
                  if k in existing and existing[k].emp_code != v['emp_code']]

        names = {c.id: c.name for c in Company.query.all()}
        print(f'  accounts with usable history   '
              f'{len({k[0] for k in best}):>6}')
        print(f'  account x vertical pairs       {len(best):>6}')
        print(f'    new rows                     {len(new):>6}')
        print(f'    already there, same owner    {len(same):>6}')
        print(f'    already there, DIFFERENT     {len(differ):>6}')
        for reason, n in skipped.most_common():
            print(f'  skipped: {reason:<28} {n:>6}')

        print('\n  new rows by vertical:')
        for vertical, n in Counter(v['vertical'] for v in new).most_common():
            print(f'      {vertical:<24} {n:>5}')

        if differ:
            print('\n  NOT changed — a person already set these and the '
                  'history disagrees:')
            for v, row in differ[:20]:
                print(f'      {names.get(v["account_id"], v["account_id"])[:34]:<34} '
                      f'{v["vertical"]:<20} set {row.emp_code}, history '
                      f'says {v["emp_code"]}')

        if args.check:
            print('\n  a sample of what would be written:')
            for v in new[:25]:
                print(f'      {names.get(v["account_id"], "?")[:34]:<34} '
                      f'{v["vertical"]:<20} {v["emp_code"]:<12} '
                      f'(from lead {v["from_lead"]}, {v["stage"]})')
            print('\nDRY RUN — nothing written. Re-run with --apply.')
            return

        written = 0
        try:
            for v in new:
                db.session.add(AccountVerticalOwner(
                    account_id=v['account_id'], vertical=v['vertical'],
                    emp_code=v['emp_code'], source='seeded',
                    note=f'from lead {v["from_lead"]} ({v["stage"]})',
                    updated_by='CRM-AUTO-ASSIGN-01'))
                written += 1
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'\n  !! ROLLED BACK: {type(exc).__name__}: '
                  f'{str(exc)[:200]}')
            raise SystemExit(1)
        print(f'\n  {written} row(s) written, in one transaction. Rows a '
              f'person had already set were left alone.')


if __name__ == '__main__':
    main()
