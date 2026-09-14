"""
Lead commercial values: opportunity value, quote block, currency.

Adds (all nullable) to leads
    value_currency, opportunity_value_num, opportunity_fx_rate, value_basis,
    quote_no, quote_value_num, quote_fx_rate, quote_date (indexed),
    quote_validity_date, quote_cost_num, quote_revision, quote_revisions,
    quote_recorded_by, quote_recorded_at
and registers the Master Data list 'fx_rate' (Exchange Rates).

Nothing is backfilled. The older leads.cost_million is the customer's
project cost (capex) from the project database, not a Procam deal value;
copying it into opportunity value would have put figures like NTPC's
₹20,000-crore plant into the pipeline. It stays where it is and the lead
shows it as "project cost".

The app adds the columns at boot as well (init_db autoheal), so a restart
before this script does not break anything.

Usage
    python scripts/2026_10_05_lead_commercial_values.py --check
    python scripts/2026_10_05_lead_commercial_values.py
    python scripts/2026_10_05_lead_commercial_values.py --down --yes

Back up first:
    .venv/bin/python scripts/backup_database.py --label pre-migration
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, '.env'))
except ImportError:
    pass

from sqlalchemy import create_engine, text          # noqa: E402

COLS = (
    ('value_currency',        "VARCHAR(3) DEFAULT 'INR'"),
    ('opportunity_value_num', 'NUMERIC(15,2)'),
    ('opportunity_fx_rate',   'NUMERIC(12,4)'),
    ('value_basis',           'VARCHAR(20)'),
    ('quote_no',              'VARCHAR(40)'),
    ('quote_value_num',       'NUMERIC(15,2)'),
    ('quote_fx_rate',         'NUMERIC(12,4)'),
    ('quote_date',            'DATE'),
    ('quote_validity_date',   'DATE'),
    ('quote_cost_num',        'NUMERIC(15,2)'),
    ('quote_revision',        'INTEGER DEFAULT 0'),
    ('quote_revisions',       'JSON'),
    ('quote_recorded_by',     'VARCHAR(20)'),
    ('quote_recorded_at',     'TIMESTAMP'),
)
INDEX = ('ix_leads_quote_date', 'quote_date')
FX_LIST = ('fx_rate', 'Exchange Rates',
           'INR per unit of each foreign currency lead values are entered in. '
           'A lead keeps the rate it was saved at.')


def _db_url():
    return os.environ.get('DATABASE_URL') or (
        'sqlite:///' + os.path.join(_ROOT, 'procam_crm.db'))


def _has_table(conn, name):
    return bool(conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"),
        {'n': name}).fetchone())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    print(f'database: {_db_url()}')
    with create_engine(_db_url()).begin() as conn:
        n = conn.execute(text('SELECT COUNT(*) FROM employees')).scalar()
        if not n:
            raise SystemExit('Refusing to run: not the production database.')
        have = {r[1] for r in conn.execute(text('PRAGMA table_info(leads)'))}

        if args.down:
            present = [c for c, _ in COLS if c in have]
            if args.check or not present:
                print(f'== DRY-RUN == WOULD drop {present or "nothing"}')
                print('  This loses every quote number, date, validity, cost '
                      'and revision history entered since.')
                return
            if not args.yes:
                raise SystemExit('  Refusing without --yes.')
            conn.execute(text(f'DROP INDEX IF EXISTS {INDEX[0]}'))
            for c in present:
                conn.execute(text(f'ALTER TABLE leads DROP COLUMN {c}'))
                print(f'  - leads.{c}')
            print('  estimated_value_inr / quoted_amount_inr are older '
                  'columns and stay.')
            return

        missing = [(c, ddl) for c, ddl in COLS if c not in have]
        fx_ready = _has_table(conn, 'master_lists')
        fx_there = fx_ready and conn.execute(text(
            'SELECT 1 FROM master_lists WHERE key = :k'),
            {'k': FX_LIST[0]}).fetchone()

        if args.check:
            print('== DRY-RUN — nothing written ==')
            print(f'  WOULD add: {[c for c, _ in missing] or "(already there)"}')
            print(f'  WOULD register Master Data list fx_rate: '
                  f'{"already there" if fx_there else "yes"}')
            print('  Nothing backfilled: cost_million is project cost, '
                  'not deal value.')
            return

        for c, ddl in missing:
            conn.execute(text(f'ALTER TABLE leads ADD COLUMN {c} {ddl}'))
            print(f'  + leads.{c}')
        conn.execute(text(f'CREATE INDEX IF NOT EXISTS {INDEX[0]} '
                          f'ON leads ({INDEX[1]})'))
        if fx_ready and not fx_there:
            conn.execute(text(
                'INSERT INTO master_lists (key, label, description, '
                'is_system, sort_order) VALUES (:k, :l, :d, 1, 999)'),
                {'k': FX_LIST[0], 'l': FX_LIST[1], 'd': FX_LIST[2]})
            print('  + master list fx_rate')

    print('  done.')


if __name__ == '__main__':
    main()
