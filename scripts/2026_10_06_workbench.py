"""
Daily Workbench — schema.

Adds
    leads.next_action   what the owner has decided to do next, in their
                        words. The date it is due is followup_date,
                        which the CRM already had.

Nothing is backfilled: nobody has written a next action yet, and
inventing one from the stage would put words in people's mouths. The
Workbench lists leads without one under "Data update required".

The app adds the column at boot as well (init_db autoheal), so a restart
before this script does not break the lead list.

Usage
    python scripts/2026_10_06_workbench.py --check
    python scripts/2026_10_06_workbench.py
    python scripts/2026_10_06_workbench.py --down --yes
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

COLS = (('next_action', 'VARCHAR(200)'),)


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
        if not conn.execute(text('SELECT COUNT(*) FROM employees')).scalar():
            raise SystemExit('Refusing to run: not the production database.')
        have = {r[1] for r in conn.execute(text('PRAGMA table_info(leads)'))}

        if args.down:
            present = [c for c, _ in COLS if c in have]
            if args.check or not present:
                print(f'== DRY-RUN == WOULD drop {present or "nothing"}')
                print('  This loses every next action people have typed.')
                return
            if not args.yes:
                raise SystemExit('  Refusing without --yes.')
            for c in present:
                conn.execute(text(f'ALTER TABLE leads DROP COLUMN {c}'))
                print(f'  - leads.{c}')
            return

        missing = [(c, ddl) for c, ddl in COLS if c not in have]
        if args.check:
            print('== DRY-RUN — nothing written ==')
            print(f'  WOULD add: {[c for c, _ in missing] or "(already there)"}')
            print('  Nothing backfilled.')
            return
        for c, ddl in missing:
            conn.execute(text(f'ALTER TABLE leads ADD COLUMN {c} {ddl}'))
            print(f'  + leads.{c}')
    print('  done.')


if __name__ == '__main__':
    main()
