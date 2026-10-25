"""
CRM-ACCOUNT-FIRST-01 — the per-account owner confirmation table.

    .venv/bin/python scripts/2026_10_25_account_first.py --check
    .venv/bin/python scripts/2026_10_25_account_first.py
    .venv/bin/python scripts/2026_10_25_account_first.py --down --yes

Creates `account_owner_confirmation` (§3.4, D4). Nothing else:
`owner_confirmation` is left exactly as it is, because its
`lead_id` is unique and every existing row answers a per-lead
question that still has an answer. Widening it would change what
those rows mean; sitting beside it costs one table.

Additive and reversible. Nothing is backfilled — a confirmation
nobody made is not a fact to invent, and the queue filling up on
day one is the honest state.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect, text                      # noqa: E402

from app import app, db                                   # noqa: E402
# Imported by name, not as `import app.models.assignment`: that
# statement rebinds `app` to the package and the Flask object is
# gone, which the script only discovers when it is actually run.
from app.models.assignment import (AccountOwnerConfirmation  # noqa: E402
                                   as _AOC)

TABLE = 'account_owner_confirmation'


def _tables():
    try:
        return set(inspect(db.engine).get_table_names())
    except Exception:                                     # noqa: BLE001
        return set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        here = TABLE in _tables()
        print(f'  {TABLE}: {"present" if here else "absent"}')

        if args.check:
            if here:
                n = db.session.execute(text(
                    f'SELECT COUNT(*) FROM {TABLE}')).scalar()
                print(f'  rows: {n}')
            # What the gate would hold the moment the flag goes on.
            for label, sql in (
                    ('open leads with no account',
                     'SELECT COUNT(*) FROM leads WHERE company_id IS '
                     'NULL AND stage NOT IN ("Won","Lost","On Hold",'
                     '"Not Interested")'),
                    ('accounts with no owner for any vertical',
                     'SELECT COUNT(*) FROM companies c WHERE NOT '
                     'EXISTS (SELECT 1 FROM account_vertical_owner o '
                     'WHERE o.account_id = c.id)')):
                try:
                    print(f'  {label}: '
                          f'{db.session.execute(text(sql)).scalar()}')
                except Exception as exc:                  # noqa: BLE001
                    print(f'  {label}: unreadable ({str(exc)[:60]})')
            print('\nCHECK ONLY — nothing written.')
            return

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            if not here:
                print('  already absent')
                return
            db.session.execute(text(f'DROP TABLE {TABLE}'))
            db.session.commit()
            print(f'  dropped {TABLE}')
            return

        if here:
            print('  already present — nothing to do')
            return
        _AOC.__table__.create(db.engine)
        print(f'  created {TABLE}')
        print('\n  Done. Nothing backfilled: a confirmation nobody '
              'made is not a fact to invent.')


if __name__ == '__main__':
    main()
