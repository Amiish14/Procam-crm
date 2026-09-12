"""
Label the email trail with the class that filed it — §7.

Rate-sourcing mail already lands on lead_emails; it just is not marked,
so it cannot be told apart from the customer conversation. One column
turns the §7 "Rate Sourcing tab" into a filter over rows we already
hold, rather than a second table holding the same emails twice.

Adds
    lead_emails.intake_class

Backfills from the classification log where a row can be matched on its
message id, so trails filed before this keep their labels.

Usage
    python scripts/2026_09_28_rate_sourcing_tab.py --check
    python scripts/2026_09_28_rate_sourcing_tab.py
    python scripts/2026_09_28_rate_sourcing_tab.py --down --yes
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
        have = _cols(conn, 'lead_emails')

        if args.down:
            if 'intake_class' not in have:
                print('  nothing to drop.')
                return
            print('  This DISCARDS the class label on every trail row.')
            if args.check:
                print('== DRY-RUN — nothing written ==')
                return
            if not args.yes:
                raise SystemExit('  Refusing without --yes.')
            conn.execute(text('DROP INDEX IF EXISTS ix_lead_emails_intake_class'))
            conn.execute(text('ALTER TABLE lead_emails DROP COLUMN intake_class'))
            print('  - lead_emails.intake_class')
            return

        rows = conn.execute(text(
            'SELECT COUNT(*) FROM lead_emails')).scalar() or 0
        matchable = conn.execute(text(
            'SELECT COUNT(*) FROM lead_emails e '
            'WHERE e.message_id IS NOT NULL AND EXISTS ('
            '  SELECT 1 FROM email_classifications c '
            '  WHERE c.message_id = e.message_id)')).scalar() or 0

        if args.check:
            print('== DRY-RUN — nothing written ==')
            print(f'  WOULD add: '
                  f'{"intake_class" if "intake_class" not in have else "(already there)"}')
            print(f'  trail rows: {rows}')
            print(f'  WOULD backfill a label on: {matchable}')
            return

        if 'intake_class' not in have:
            conn.execute(text(
                'ALTER TABLE lead_emails ADD COLUMN intake_class VARCHAR(32)'))
            print('  + lead_emails.intake_class')
        conn.execute(text('CREATE INDEX IF NOT EXISTS '
                          'ix_lead_emails_intake_class '
                          'ON lead_emails (intake_class)'))

        n = conn.execute(text(
            'UPDATE lead_emails SET intake_class = ('
            '  SELECT c.classification FROM email_classifications c '
            '  WHERE c.message_id = lead_emails.message_id LIMIT 1) '
            'WHERE intake_class IS NULL AND message_id IS NOT NULL')).rowcount
        print(f'  backfilled {n} trail row(s) from the classification log')
    print('  done.')


if __name__ == '__main__':
    main()
