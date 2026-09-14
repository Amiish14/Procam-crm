"""
Lead commercial values: opportunity value, quote block, currency.

Adds (all nullable) to leads
    value_currency, opportunity_value_num, opportunity_fx_rate, value_basis,
    quote_no, quote_value_num, quote_fx_rate, quote_date (indexed),
    quote_validity_date, quote_cost_num, quote_revision, quote_revisions,
    quote_recorded_by, quote_recorded_at
and registers the Master Data list 'fx_rate' (Exchange Rates).

Backfill (--backfill, separate, previewed by --check)
    Leads whose only value is the old cost_million (₹ millions) get it in
    rupees: estimated_value_inr and opportunity_value_num =
    cost_million × 10,00,000, currency INR. Only where estimated_value_inr
    is empty; cost_million itself is not touched. The ids changed are
    written to backups/lead_value_backfill-<time>.json, and
    --undo-backfill <file> clears exactly those again.

    quoted_amount_inr is never backfilled: amounts read from emails are
    shown to the PIC as a suggestion, not counted.

The app adds the columns at boot as well (init_db autoheal), so a restart
before this script does not break anything.

Usage
    python scripts/2026_10_05_lead_commercial_values.py --check
    python scripts/2026_10_05_lead_commercial_values.py
    python scripts/2026_10_05_lead_commercial_values.py --backfill
    python scripts/2026_10_05_lead_commercial_values.py --undo-backfill backups/lead_value_backfill-….json
    python scripts/2026_10_05_lead_commercial_values.py --down --yes

Back up first:
    .venv/bin/python scripts/backup_database.py --label pre-migration
"""
import argparse
import json
import os
import sys
from datetime import datetime

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

_BACKFILL_WHERE = ('cost_million > 0 AND (estimated_value_inr IS NULL '
                   'OR estimated_value_inr = 0)')


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
    ap.add_argument('--backfill', action='store_true')
    ap.add_argument('--undo-backfill', metavar='FILE')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    print(f'database: {_db_url()}')
    with create_engine(_db_url()).begin() as conn:
        n = conn.execute(text('SELECT COUNT(*) FROM employees')).scalar()
        if not n:
            raise SystemExit('Refusing to run: not the production database.')
        have = {r[1] for r in conn.execute(text('PRAGMA table_info(leads)'))}

        if args.undo_backfill:
            with open(args.undo_backfill) as fh:
                ids = json.load(fh)['lead_ids']
            if args.check:
                print(f'== DRY-RUN == WOULD clear the backfilled value on '
                      f'{len(ids)} lead(s)')
                return
            cleared = 0
            for lid in ids:
                cleared += conn.execute(text(
                    'UPDATE leads SET estimated_value_inr = NULL, '
                    'opportunity_value_num = NULL WHERE id = :i AND '
                    'ROUND(estimated_value_inr) = ROUND(cost_million * 1000000)'),
                    {'i': lid}).rowcount
            print(f'  cleared {cleared} of {len(ids)} (leads edited since '
                  f'the backfill are left as they are)')
            return

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
        backfill_n = conn.execute(text(
            f'SELECT COUNT(*) FROM leads WHERE {_BACKFILL_WHERE}')).scalar()
        fx_ready = _has_table(conn, 'master_lists')
        fx_there = fx_ready and conn.execute(text(
            'SELECT 1 FROM master_lists WHERE key = :k'),
            {'k': FX_LIST[0]}).fetchone()

        if args.check:
            print('== DRY-RUN — nothing written ==')
            print(f'  WOULD add: {[c for c, _ in missing] or "(already there)"}')
            print(f'  WOULD register Master Data list fx_rate: '
                  f'{"already there" if fx_there else "yes"}')
            print(f'  --backfill WOULD value {backfill_n} lead(s) in rupees '
                  f'from their ₹ million figure')
            for row in conn.execute(text(
                    f'SELECT id, company, cost_million FROM leads '
                    f'WHERE {_BACKFILL_WHERE} ORDER BY cost_million DESC '
                    f'LIMIT 10')):
                print(f'      #{row[0]:<6} {str(row[1])[:40]:<40} '
                      f'₹{row[2]} M → ₹ {row[2] * 10:.1f} L')
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

        if args.backfill:
            ids = [r[0] for r in conn.execute(text(
                f'SELECT id FROM leads WHERE {_BACKFILL_WHERE}'))]
            if ids:
                os.makedirs(os.path.join(_ROOT, 'backups'), exist_ok=True)
                path = os.path.join(
                    _ROOT, 'backups', 'lead_value_backfill-'
                    + datetime.utcnow().strftime('%Y%m%d-%H%M%S') + '.json')
                with open(path, 'w') as fh:
                    json.dump({'lead_ids': ids,
                               'at': datetime.utcnow().isoformat()}, fh)
                conn.execute(text(
                    f"UPDATE leads SET estimated_value_inr = "
                    f"ROUND(cost_million * 1000000, 2), opportunity_value_num = "
                    f"ROUND(cost_million * 1000000, 2), value_currency = "
                    f"COALESCE(value_currency, 'INR') WHERE {_BACKFILL_WHERE}"))
                print(f'  backfilled {len(ids)} lead(s); ids in {path}')
            else:
                print('  backfill: nothing to do')
    print('  done.')


if __name__ == '__main__':
    main()
