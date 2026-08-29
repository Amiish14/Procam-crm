"""
v2026-08 — CRM enhancement pack migration.

Adds five new nullable columns to `leads`:
    website              VARCHAR(200)
    subsidiaries         TEXT
    estimated_value_inr  NUMERIC(15,2)
    quoted_amount_inr    NUMERIC(15,2)
    relevance            VARCHAR(20) DEFAULT 'Undecided'

Additive, idempotent, no data loss.

Usage:
    python scripts/2026_08_29_lead_enhancement_pack.py           # dry-run
    python scripts/2026_08_29_lead_enhancement_pack.py --apply
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect, text
from app import app, db

NEW_COLS = [
    ('website',              'VARCHAR(200)'),
    ('subsidiaries',         'TEXT'),
    ('estimated_value_inr',  'NUMERIC(15,2)'),
    ('quoted_amount_inr',    'NUMERIC(15,2)'),
    ('relevance',            "VARCHAR(20) DEFAULT 'Undecided'"),
]


def main(apply_changes: bool):
    with app.app_context():
        insp = inspect(db.engine)
        if not insp.has_table('leads'):
            print('leads table not present — nothing to do')
            return
        existing = {c['name'] for c in insp.get_columns('leads')}
        planned = [(n, t) for n, t in NEW_COLS if n not in existing]
        if not planned:
            print('All new columns already present — nothing to do.')
            return
        for n, t in planned:
            print(f'  + ALTER TABLE leads ADD COLUMN {n} {t}')
        if not apply_changes:
            print('\nDRY RUN — re-run with --apply.')
            return
        with db.engine.begin() as conn:
            for n, t in planned:
                conn.execute(text(f'ALTER TABLE leads ADD COLUMN {n} {t}'))
                print(f'  [OK] added {n}')
        print('\nMigration complete.')


if __name__ == '__main__':
    main(apply_changes='--apply' in sys.argv)
