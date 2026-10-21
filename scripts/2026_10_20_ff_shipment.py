"""
CRM 2.0 R5 — the shipment on the enquiry, and how a customer is
billed.

    .venv/bin/python scripts/2026_10_20_ff_shipment.py --check
    .venv/bin/python scripts/2026_10_20_ff_shipment.py
    .venv/bin/python scripts/2026_10_20_ff_shipment.py --down --yes

  rfqs.direction and the shipment block   TMS Part A, captured once
  quotes.incoterm_place                   completes the Incoterm
  quotes.gst_treatment                    this job only
  companies.default_gst_treatment         this customer, every job

Additive, nothing backfilled. Existing enquiries keep a null
direction, which is correct: nobody has said, and the handover pack
leaves the field out so the TMS refuses rather than importing a job
pointing the wrong way through customs.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect, text                      # noqa: E402

from app import app, db                                   # noqa: E402

COLUMNS = (
    ('rfqs', 'direction', 'VARCHAR(12)'),
    ('rfqs', 'shipper_name', 'VARCHAR(200)'),
    ('rfqs', 'shipper_address', 'TEXT'),
    ('rfqs', 'consignee_name', 'VARCHAR(200)'),
    ('rfqs', 'consignee_address', 'TEXT'),
    ('rfqs', 'pol_code', 'VARCHAR(10)'),
    ('rfqs', 'pol_name', 'VARCHAR(120)'),
    ('rfqs', 'pod_code', 'VARCHAR(10)'),
    ('rfqs', 'pod_name', 'VARCHAR(120)'),
    ('rfqs', 'hs_code', 'VARCHAR(20)'),
    ('rfqs', 'package_count', 'INTEGER'),
    ('rfqs', 'package_kind', 'VARCHAR(60)'),
    ('rfqs', 'gross_weight_kg', 'NUMERIC(14,3)'),
    ('rfqs', 'volume_cbm', 'NUMERIC(12,3)'),
    ('rfqs', 'container_count', 'INTEGER'),
    ('rfqs', 'is_dangerous', 'BOOLEAN'),
    ('rfqs', 'dg_class', 'VARCHAR(20)'),
    ('rfqs', 'un_number', 'VARCHAR(12)'),
    ('quotes', 'incoterm_place', 'VARCHAR(160)'),
    ('quotes', 'gst_treatment', 'VARCHAR(24)'),
    ('companies', 'default_gst_treatment', 'VARCHAR(24)'),
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
        absent = [(t, c) for t, c, _d in COLUMNS if c not in _cols(t)]
        print(f'  columns: {len(COLUMNS) - len(absent)} present, '
              f'{len(absent)} absent')
        for tbl, col in absent:
            print(f'    absent: {tbl}.{col}')

        if args.check:
            if 'direction' in _cols('rfqs'):
                n = db.session.execute(text(
                    'SELECT COUNT(*) FROM rfqs '
                    'WHERE direction IS NOT NULL')).scalar()
                print(f'\n  enquiries with a direction: {n} '
                      f'of {db.session.execute(text("SELECT COUNT(*) "
                                                    "FROM rfqs")).scalar()}')
            print('\nCHECK ONLY — nothing written.')
            return

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            for tbl, col, _d in reversed(COLUMNS):
                if col not in _cols(tbl):
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
                continue
            db.session.execute(text(
                f'ALTER TABLE {tbl} ADD COLUMN {col} {ddl}'))
            db.session.commit()
            print(f'  added {tbl}.{col}')
        print('\n  Done. Nothing backfilled: an enquiry with no '
              'direction has not been asked, and the pack leaves '
              'the field out rather than guessing.')


if __name__ == '__main__':
    main()
