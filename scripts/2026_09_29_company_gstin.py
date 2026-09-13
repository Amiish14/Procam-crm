"""
Identify an account by its GSTIN — §13, resolution path 5.

The brief lists five ways to recognise which account an email belongs
to. Four were built: contact email, account email domain, account
website, company name. The fifth — the GST / customer master — had
nowhere to live: companies has no GSTIN column at all.

Adds
    companies.gstin           the 15-character registration
    companies.gstin_source    where it came from, so a scraped value is
                              not mistaken for one an admin confirmed

Nothing is backfilled. A GSTIN guessed out of old email bodies and
written to the master would be a number nobody checked, attached to an
account people trust. They are captured going forward, against the
message, and an admin promotes one to the account deliberately.

Usage
    python scripts/2026_09_29_company_gstin.py --check
    python scripts/2026_09_29_company_gstin.py
    python scripts/2026_09_29_company_gstin.py --down --yes
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


def _db_url():
    return os.environ.get('DATABASE_URL') or (
        'sqlite:///' + os.path.join(_ROOT, 'procam_crm.db'))


def _guard(conn):
    try:
        n = conn.execute(text('SELECT COUNT(*) FROM employees')).scalar()
    except Exception:
        n = 0
    if not n:
        raise SystemExit('Refusing to run: not the production database.')
    return n


def _cols(conn, t):
    return {r[1] for r in conn.execute(text(f'PRAGMA table_info({t})'))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    print(f'database: {_db_url()}')
    engine = create_engine(_db_url())
    with engine.begin() as conn:
        print(f'  employees: {_guard(conn)}')
        have = _cols(conn, 'companies')

        if args.down:
            present = [c for c in ('gstin', 'gstin_source') if c in have]
            if not present:
                print('  nothing to drop.')
                return
            filled = conn.execute(text(
                'SELECT COUNT(*) FROM companies '
                "WHERE gstin IS NOT NULL AND gstin != ''")).scalar() or 0
            print(f'  This DISCARDS the GSTIN on {filled} account(s).')
            if args.check:
                print('== DRY-RUN — nothing written ==')
                return
            if not args.yes:
                raise SystemExit('  Refusing without --yes.')
            conn.execute(text('DROP INDEX IF EXISTS ix_companies_gstin'))
            for col in present:
                conn.execute(text(
                    f'ALTER TABLE companies DROP COLUMN {col}'))
                print(f'  - companies.{col}')
            return

        total = conn.execute(text(
            'SELECT COUNT(*) FROM companies')).scalar() or 0
        adding = [c for c in ('gstin', 'gstin_source') if c not in have]

        if args.check:
            print('== DRY-RUN — nothing written ==')
            print(f'  accounts: {total}')
            print(f'  WOULD add: {", ".join(adding) or "(already there)"}')
            print('  WOULD backfill: nothing. A GSTIN read out of an old '
                  'email\n  and written to the master is a number nobody '
                  'checked.')
            return

        if 'gstin' not in have:
            conn.execute(text(
                'ALTER TABLE companies ADD COLUMN gstin VARCHAR(15)'))
            print('  + companies.gstin')
        if 'gstin_source' not in have:
            conn.execute(text(
                'ALTER TABLE companies ADD COLUMN gstin_source VARCHAR(30)'))
            print('  + companies.gstin_source')
        conn.execute(text('CREATE INDEX IF NOT EXISTS ix_companies_gstin '
                          'ON companies (gstin)'))
        print('  index on companies.gstin')
    print('  done. Nothing was backfilled, by design.')


if __name__ == '__main__':
    main()
