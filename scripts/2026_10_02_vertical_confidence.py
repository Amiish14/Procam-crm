"""
Keep the vertical recommendation's confidence and reason — §17, Phase 2.

Adds
    leads.vertical_confidence   0-100, the margin-based confidence
    leads.vertical_reason       the terms that decided it

Nothing is backfilled: the confidence was never stored, and recomputing
it for old leads would present today's vocabulary as the reason for a
decision made months ago. New leads carry it from ingest onward.

The app also adds these at boot (see init_db's autoheal), so a restart
before this script runs does not break the lead list. This script is the
documented --check and --down.

Usage
    python scripts/2026_10_02_vertical_confidence.py --check
    python scripts/2026_10_02_vertical_confidence.py
    python scripts/2026_10_02_vertical_confidence.py --down --yes
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

COLS = (('vertical_confidence', 'INTEGER'),
        ('vertical_reason', 'VARCHAR(200)'))


def _db_url():
    return os.environ.get('DATABASE_URL') or (
        'sqlite:///' + os.path.join(_ROOT, 'procam_crm.db'))


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
            if not present:
                print('  nothing to drop.')
                return
            if args.check:
                print(f'== DRY-RUN == WOULD drop {present}')
                return
            if not args.yes:
                raise SystemExit('  Refusing without --yes.')
            for c in present:
                conn.execute(text(f'ALTER TABLE leads DROP COLUMN {c}'))
                print(f'  - leads.{c}')
            return

        missing = [c for c, _ in COLS if c not in have]
        if args.check:
            print('== DRY-RUN — nothing written ==')
            print(f'  WOULD add: {missing or "(already there)"}')
            print('  Nothing backfilled.')
            return
        for c, ddl in COLS:
            if c not in have:
                conn.execute(text(f'ALTER TABLE leads ADD COLUMN {c} {ddl}'))
                print(f'  + leads.{c}')
    print('  done.')


if __name__ == '__main__':
    main()
