"""
CRM-JUNK-DELETE-01 — the columns behind "Delete – not relevant".

    .venv/bin/python scripts/2026_10_28_junk_delete.py --check
    .venv/bin/python scripts/2026_10_28_junk_delete.py
    .venv/bin/python scripts/2026_10_28_junk_delete.py --down --yes

Additive, and safe to run twice. Nothing is backfilled: no existing
lead is junk until a person says so, and a rule that guessed would
be the 967-lead incident with better manners.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect, text                      # noqa: E402

from app import app, db                                   # noqa: E402

COLUMNS = (
    ('leads', 'junk_deleted', 'BOOLEAN'),
    ('leads', 'junk_deleted_at', 'DATETIME'),
    ('leads', 'junk_deleted_by', 'VARCHAR(20)'),
    ('leads', 'junk_reason', 'VARCHAR(60)'),
    ('leads', 'junk_reason_note', 'VARCHAR(400)'),
)
INDEX = ('ix_leads_junk_deleted', 'leads', 'junk_deleted')


def _cols(table):
    try:
        return {c['name'] for c in inspect(db.engine).get_columns(table)}
    except Exception:                                     # noqa: BLE001
        return set()


def _indexes(table):
    try:
        return {i['name'] for i in inspect(db.engine).get_indexes(table)}
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
        absent = [(t, c) for t, c, _d in COLUMNS if c not in _cols(t)]
        name, table, _col = INDEX
        has_index = name in _indexes(table)
        print(f'  columns: {len(COLUMNS) - len(absent)} present, '
              f'{len(absent)} absent')
        print(f'  {name}: {"present" if has_index else "absent"}')

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            # The leads themselves are untouched: dropping these
            # columns puts every deleted lead back on the board,
            # which is the right way round for a reversal.
            if has_index:
                db.session.execute(text(f'DROP INDEX {name}'))
                db.session.commit()
                print(f'  dropped {name}')
            for tbl, col, _d in reversed(COLUMNS):
                if col not in _cols(tbl):
                    continue
                try:
                    db.session.execute(text(
                        f'ALTER TABLE {tbl} DROP COLUMN {col}'))
                    db.session.commit()
                    print(f'  dropped {tbl}.{col}')
                except Exception as exc:                  # noqa: BLE001
                    db.session.rollback()
                    print(f'  !! {tbl}.{col}: {str(exc)[:110]}')
            return

        if args.check:
            print('\nCHECK ONLY — nothing written.')
            return

        for tbl, col, ddl in COLUMNS:
            if col in _cols(tbl):
                continue
            db.session.execute(text(
                f'ALTER TABLE {tbl} ADD COLUMN {col} {ddl}'))
            db.session.commit()
            print(f'  added {tbl}.{col}')
        if not has_index:
            db.session.execute(text(
                f'CREATE INDEX {name} ON {table} ({_col})'))
            db.session.commit()
            print(f'  created {name}')
        print('\n  Done. Nothing is marked junk: FEATURE_JUNK_DELETE is '
              'off, and the first delete is somebody clicking it.')


if __name__ == '__main__':
    main()
