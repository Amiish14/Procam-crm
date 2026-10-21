"""
Decisions of 21 Oct 2026 (CRM questionnaire) — the columns they need.

    .venv/bin/python scripts/2026_10_21_crm_decisions.py --check
    .venv/bin/python scripts/2026_10_21_crm_decisions.py
    .venv/bin/python scripts/2026_10_21_crm_decisions.py --down --yes

  leads.origin              VARCHAR(200)   B6: route for transport/freight
  leads.destination         VARCHAR(200)
  leads.site_location       VARCHAR(200)   B6: site for warehousing/installation
  quote_lines.vehicle_source VARCHAR(10)   H1: PTM-Own / PTM-Market / Mechanical

All additive and nullable; existing rows are untouched. The settings the
decisions need (tender desk, margin minimums, approval limit, viewers,
validity, subject prefix) have defaults in app/services/admin_settings.py
and need no migration. --down drops the four columns and with them any
origin, destination, site or vehicle values typed in since.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect, text                      # noqa: E402

from app import app, db                                   # noqa: E402

COLUMNS = (('leads', 'origin', 'VARCHAR(200)'),
           ('leads', 'destination', 'VARCHAR(200)'),
           ('leads', 'site_location', 'VARCHAR(200)'),
           ('quote_lines', 'vehicle_source', 'VARCHAR(10)'))


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
        from app.models.quote import QuoteLine               # noqa: F401
        print(f'database: {db.engine.url}')
        for tbl, col, _d in COLUMNS:
            print(f'  {tbl}.{col}: '
                  + ('present' if col in _cols(tbl) else 'absent'))

        if args.check:
            print('\nCHECK ONLY — nothing written.')
            return
        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            for tbl, col, _d in COLUMNS:
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
