"""
CRM 2.0 R5 / TMS intake — the customer's own PO number.

    .venv/bin/python scripts/2026_10_20_client_po_number.py --check
    .venv/bin/python scripts/2026_10_20_client_po_number.py
    .venv/bin/python scripts/2026_10_20_client_po_number.py --down --yes

  ff_handover_packs.client_po_number   VARCHAR(60)

One column, additive. Both TMS intake formats carry the customer's
own number and neither generates it: Part A calls it
`tally_po_number`, Part B `client_po_number`, and in both it sits
beside Procam's number and is never used as one. Entering one the
customer has already used sends the TMS user to the existing order
rather than making a second, so it has to be the number they
actually quoted.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect, text                      # noqa: E402

from app import app, db                                   # noqa: E402

COLUMNS = (('ff_handover_packs', 'client_po_number', 'VARCHAR(60)'),)


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
        from app.models.ff import FfHandoverPack             # noqa: F401
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
