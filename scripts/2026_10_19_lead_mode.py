"""
`leads.mode` — Sea / Air / Multimodal on Freight Forwarding leads.

    .venv/bin/python scripts/2026_10_19_lead_mode.py --check
    .venv/bin/python scripts/2026_10_19_lead_mode.py
    .venv/bin/python scripts/2026_10_19_lead_mode.py --down --yes

One nullable column. Nothing is written into it here: the backfill
reads the audit trail and lives in `scripts/backfill_lead_mode.py`,
because it is a judgement about history and this is not.

Additive and idempotent — running it twice is a no-op, and `--down`
drops the column again. SQLite before 3.35 cannot DROP COLUMN; the
down path says so rather than half-finishing.

One honest caveat. `init_db()` carries an additive-column autoheal
and runs on import, so by the time this script's own code executes,
the column is already there and it reports "Already present". That is
not the script failing — it is the autoheal doing its job — but it
does mean `--down` only sticks until the next process starts. To take
the column off for good, remove it from the `_adds` list in `app.py`
and from the model first. The script is still worth running before a
restart: it adds the column under the old code, so the window where
a running worker has a model column the database lacks never opens.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect, text                      # noqa: E402

from app import app, db                                   # noqa: E402

TABLE, COLUMN, DDL = 'leads', 'mode', 'VARCHAR(20)'


def _has_column():
    cols = {c['name'] for c in inspect(db.engine).get_columns(TABLE)}
    return COLUMN in cols


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        present = _has_column()
        print(f'{TABLE}.{COLUMN}: ' + ('present' if present else 'absent'))

        if args.check:
            if present:
                filled = db.session.execute(text(
                    f'SELECT COUNT(*) FROM {TABLE} '
                    f'WHERE {COLUMN} IS NOT NULL AND {COLUMN} != ""'
                )).scalar()
                print(f'  rows with a mode set: {filled}')
            print('\nCHECK ONLY — nothing written.')
            return

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            if not present:
                print('Already absent. Nothing to do.')
                return
            try:
                db.session.execute(text(
                    f'ALTER TABLE {TABLE} DROP COLUMN {COLUMN}'))
                db.session.commit()
            except Exception as exc:                      # noqa: BLE001
                db.session.rollback()
                print(f'  !! could not drop: {type(exc).__name__}: '
                      f'{str(exc)[:200]}')
                print('  SQLite below 3.35 has no DROP COLUMN. The column '
                      'is nullable and unread by older code; leaving it '
                      'in place is harmless.')
                raise SystemExit(1)
            print(f'Dropped {TABLE}.{COLUMN}.')
            return

        if present:
            print('Already present. Nothing to do.')
            return
        db.session.execute(text(
            f'ALTER TABLE {TABLE} ADD COLUMN {COLUMN} {DDL}'))
        db.session.commit()
        print(f'Added {TABLE}.{COLUMN} {DDL}.')


if __name__ == '__main__':
    main()
