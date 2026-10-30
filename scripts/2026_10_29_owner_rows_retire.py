"""
FINISH-TRACKER-CRM §3 — an owner row is retired, never deleted.

    .venv/bin/python scripts/2026_10_29_owner_rows_retire.py --check
    .venv/bin/python scripts/2026_10_29_owner_rows_retire.py
    .venv/bin/python scripts/2026_10_29_owner_rows_retire.py --down --yes

  account_vertical_owner.active       BOOLEAN, default 1

One column, additive. Every existing row is live, so the column is
backfilled to 1 and nothing changes behaviour on the way in.

Why
    Rolling back the TKT-0069 owner seed was a DELETE of every row
    marked `seeded`. That is the one irreversible step in an
    otherwise reversible release: those rows are the CRM's reading
    of who has actually served each account, derived from lead
    history, and a delete throws away both the reading and the fact
    that it was ever made. Re-running the seed afterwards does not
    restore them — the lead history it reads has moved on.

    With this column the rollback marks them inactive instead.
    `auto_assign.owner_for_vertical` ignores inactive rows, so the
    effect is identical, and turning the seed back on is one UPDATE
    rather than a re-derivation.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect, text                      # noqa: E402

from app import app, db                                   # noqa: E402

TABLE = 'account_vertical_owner'
COLUMN = 'active'
DDL = 'BOOLEAN DEFAULT 1'


def _cols(table):
    try:
        return {c['name'] for c in inspect(db.engine).get_columns(table)}
    except Exception:
        return set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        here = COLUMN in _cols(TABLE)
        print(f'  {TABLE}.{COLUMN}: ' + ('present' if here else 'absent'))

        if args.check:
            if here:
                live = db.session.execute(text(
                    f'SELECT COUNT(*) FROM {TABLE} '
                    f'WHERE {COLUMN} IS NOT 0')).scalar()
                retired = db.session.execute(text(
                    f'SELECT COUNT(*) FROM {TABLE} '
                    f'WHERE {COLUMN} IS 0')).scalar()
                print(f'\n  owner rows live:    {live}')
                print(f'  owner rows retired: {retired}')
            print('\nCHECK ONLY — nothing written.')
            return

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            if not here:
                print(f'  {TABLE}.{COLUMN} already absent.')
                return
            # Everything the column was hiding becomes visible again,
            # which is the honest down: dropping it cannot keep a
            # distinction the schema no longer has room for.
            try:
                db.session.execute(text(
                    f'ALTER TABLE {TABLE} DROP COLUMN {COLUMN}'))
                db.session.commit()
                print(f'  dropped {TABLE}.{COLUMN} — every owner row '
                      f'is live again')
            except Exception as exc:                  # noqa: BLE001
                db.session.rollback()
                print(f'  !! {TABLE}.{COLUMN}: {str(exc)[:120]}')
            return

        if here:
            print(f'  {TABLE}.{COLUMN} already present.')
        else:
            db.session.execute(text(
                f'ALTER TABLE {TABLE} ADD COLUMN {COLUMN} {DDL}'))
            db.session.commit()
            print(f'  added {TABLE}.{COLUMN}')
        filled = db.session.execute(text(
            f'UPDATE {TABLE} SET {COLUMN} = 1 '
            f'WHERE {COLUMN} IS NULL')).rowcount
        db.session.commit()
        print(f'  backfilled {filled or 0} row(s) to live')


if __name__ == '__main__':
    main()
