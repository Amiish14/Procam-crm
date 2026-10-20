"""
FF-COMMS-01 R1 — the rate-request table and the columns around it.

    .venv/bin/python scripts/2026_10_20_ff_rates.py --check
    .venv/bin/python scripts/2026_10_20_ff_rates.py
    .venv/bin/python scripts/2026_10_20_ff_rates.py --down --yes

  ff_rate_requests                      new table
  quote_lines.charge_group              which bucket a sell line is
  quote_lines.cost_amount/currency/inr  what the leg costs us
  quote_lines.source_rate_request_id    the reply it was priced from
  quotes.incoterm, quotes.client_party  what the Incoterm bills
  rate_sourcing_lines.cha_mode          own at Chennai/Mumbai
  rate_sourcing_lines.charge_group

Additive. Nothing is backfilled: no RFQ or quote exists on this
system yet — both tables have been built and never used — so there
is nothing to migrate, only room to make.

Everything it enables sits behind FEATURE_FF_CRM, which is off.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect, text                      # noqa: E402

from app import app, db                                   # noqa: E402

TABLE = 'ff_rate_requests'
COLUMNS = (
    ('quote_lines', 'charge_group', 'VARCHAR(24)'),
    ('quote_lines', 'cost_amount', 'NUMERIC(15,2)'),
    ('quote_lines', 'cost_currency', 'VARCHAR(6)'),
    ('quote_lines', 'cost_inr', 'NUMERIC(15,2)'),
    ('quote_lines', 'source_rate_request_id', 'INTEGER'),
    ('quotes', 'incoterm', 'VARCHAR(8)'),
    ('quotes', 'client_party', 'VARCHAR(12)'),
    ('rate_sourcing_lines', 'cha_mode', 'VARCHAR(12)'),
    ('rate_sourcing_lines', 'charge_group', 'VARCHAR(24)'),
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
        from app.models.ff import RateRequest           # noqa: F401
        print(f'database: {db.engine.url}')
        tables = set(inspect(db.engine).get_table_names())
        print(f'  {TABLE}: ' + ('present' if TABLE in tables else 'absent'))
        for tbl, col, _d in COLUMNS:
            print(f'  {tbl}.{col}: '
                  + ('present' if col in _cols(tbl) else 'absent'))

        if args.check:
            if TABLE in tables:
                n = db.session.execute(
                    text(f'SELECT COUNT(*) FROM {TABLE}')).scalar()
                print(f'\n  {TABLE}: {n} row(s)')
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
            if TABLE in tables:
                RateRequest.__table__.drop(db.engine)
                print(f'  dropped {TABLE}')
            return

        if TABLE not in tables:
            RateRequest.__table__.create(db.engine)
            print(f'  created {TABLE}')
        else:
            print(f'  {TABLE} already present.')
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
