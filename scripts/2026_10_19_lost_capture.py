"""
`leads.lost_to_competitor_id`, `debrief_note` and `hold_note`.

    .venv/bin/python scripts/2026_10_19_lost_capture.py --check
    .venv/bin/python scripts/2026_10_19_lost_capture.py
    .venv/bin/python scripts/2026_10_19_lost_capture.py --down --yes

Three nullable columns. Two hold what the retired
`deal.lost.debrief` and `competitor.log` tasks used to ask for; the
third holds the On Hold remark, which that dialog has always
collected and the decision route never read.

Nothing is backfilled. The tasks were never done and the remarks
were never stored, so there is nothing to recover — they start empty
and fill from the dialogs.

The usual autoheal caveat — `init_db()` adds missing columns on
import, so this reports "already present" when run from the project.
It still matters: it adds them under the old code, before the
restart.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect, text                      # noqa: E402

from app import app, db                                   # noqa: E402

TABLE = 'leads'
COLUMNS = (('lost_to_competitor_id', 'INTEGER'),
           ('debrief_note', 'TEXT'),
           ('hold_note', 'TEXT'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    with app.app_context():
        have = {c['name'] for c in inspect(db.engine).get_columns(TABLE)}
        print(f'database: {db.engine.url}')
        for name, _d in COLUMNS:
            print(f'  {TABLE}.{name}: '
                  + ('present' if name in have else 'absent'))

        if args.check:
            for name, _d in COLUMNS:
                if name in have:
                    n = db.session.execute(text(
                        f'SELECT COUNT(*) FROM {TABLE} '
                        f'WHERE {name} IS NOT NULL')).scalar()
                    print(f'    rows with {name} set: {n}')
            print('\nCHECK ONLY — nothing written.')
            return

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            for name, _d in COLUMNS:
                if name not in have:
                    continue
                try:
                    db.session.execute(text(
                        f'ALTER TABLE {TABLE} DROP COLUMN {name}'))
                    db.session.commit()
                    print(f'  dropped {name}')
                except Exception as exc:                  # noqa: BLE001
                    db.session.rollback()
                    print(f'  !! could not drop {name}: '
                          f'{type(exc).__name__}: {str(exc)[:150]}')
            return

        for name, ddl in COLUMNS:
            if name in have:
                print(f'  {name} already present.')
                continue
            db.session.execute(text(
                f'ALTER TABLE {TABLE} ADD COLUMN {name} {ddl}'))
            db.session.commit()
            print(f'  added {name}')


if __name__ == '__main__':
    main()
