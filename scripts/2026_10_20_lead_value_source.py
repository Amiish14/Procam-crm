"""
CRM-VALUE-01 / CRM 2.0 — where a lead's number came from.

    .venv/bin/python scripts/2026_10_20_lead_value_source.py --check
    .venv/bin/python scripts/2026_10_20_lead_value_source.py
    .venv/bin/python scripts/2026_10_20_lead_value_source.py --down --yes

  leads.value_source      'opportunity', 'owner', …
  leads.value_confirmed   has a person agreed with it

Additive, nothing backfilled here. A lead that already carries a
value gets neither column set, which reads as "typed by somebody,
unremarkable" — the same thing it meant yesterday.

The columns exist because copying 118 figures across from the
opportunities table is worth doing and is not the same as those
owners having given a number. Without the distinction the value
gate would start passing leads on the strength of a figure nobody
has looked at, and the one safeguard that made owners type a number
would quietly stop working.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect, text                      # noqa: E402

from app import app, db                                   # noqa: E402

COLUMNS = (
    ('leads', 'value_source', 'VARCHAR(24)'),
    ('leads', 'value_confirmed', 'BOOLEAN'),
)


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
        for tbl, col, _d in COLUMNS:
            print(f'  {tbl}.{col}: '
                  + ('present' if col in _cols(tbl) else 'absent'))

        if args.check:
            if 'value_source' in _cols('leads'):
                n = db.session.execute(text(
                    'SELECT COUNT(*) FROM leads '
                    'WHERE value_source IS NOT NULL')).scalar()
                m = db.session.execute(text(
                    'SELECT COUNT(*) FROM leads '
                    'WHERE value_confirmed = 0')).scalar()
                print(f'\n  leads with a recorded source: {n}')
                print(f'  awaiting confirmation:        {m}')
            print('\nCHECK ONLY — nothing written.')
            return

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            for tbl, col, _d in reversed(COLUMNS):
                if col not in _cols(tbl):
                    print(f'  {tbl}.{col} already absent.')
                    continue
                try:
                    db.session.execute(text(
                        f'ALTER TABLE {tbl} DROP COLUMN {col}'))
                    db.session.commit()
                    print(f'  dropped {tbl}.{col}')
                except Exception as exc:              # noqa: BLE001
                    db.session.rollback()
                    print(f'  !! {tbl}.{col}: {str(exc)[:120]}')
            return

        for tbl, col, ddl in COLUMNS:
            if col in _cols(tbl):
                print(f'  {tbl}.{col} already present.')
                continue
            db.session.execute(text(
                f'ALTER TABLE {tbl} ADD COLUMN {col} {ddl}'))
            db.session.commit()
            print(f'  added {tbl}.{col}')


if __name__ == '__main__':
    main()
