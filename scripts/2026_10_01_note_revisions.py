"""
Keep every earlier version of an edited note — Family A.

Adds
    lead_notes.revisions   JSON list of replaced versions, null until the
                           note is first edited

Nothing is backfilled and no existing note changes. Notes edited before
this ran lost their earlier text at the time; that cannot be recovered
from the database and this does not pretend to.

Usage
    python scripts/2026_10_01_note_revisions.py --check
    python scripts/2026_10_01_note_revisions.py
    python scripts/2026_10_01_note_revisions.py --down --yes
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    print(f'database: {_db_url()}')
    with create_engine(_db_url()).begin() as conn:
        print(f'  employees: {_guard(conn)}')
        have = {r[1] for r in conn.execute(text(
            'PRAGMA table_info(lead_notes)'))}
        notes = conn.execute(text('SELECT COUNT(*) FROM lead_notes')).scalar()

        if args.down:
            if 'revisions' not in have:
                print('  nothing to drop.')
                return
            edited = conn.execute(text(
                'SELECT COUNT(*) FROM lead_notes WHERE revisions IS NOT NULL'
            )).scalar()
            print(f'  This DISCARDS the edit history on {edited} note(s).')
            if args.check:
                print('== DRY-RUN — nothing written ==')
                return
            if not args.yes:
                raise SystemExit('  Refusing without --yes.')
            conn.execute(text('ALTER TABLE lead_notes DROP COLUMN revisions'))
            print('  - lead_notes.revisions')
            return

        if args.check:
            print('== DRY-RUN — nothing written ==')
            print(f'  notes: {notes}')
            print(f'  WOULD add: '
                  f'{"revisions" if "revisions" not in have else "(already there)"}')
            print('  No note changes. Edits made before this cannot be '
                  'recovered.')
            return

        if 'revisions' not in have:
            conn.execute(text(
                'ALTER TABLE lead_notes ADD COLUMN revisions TEXT'))
            print('  + lead_notes.revisions')
        else:
            print('  lead_notes.revisions present')
    print('  done.')


if __name__ == '__main__':
    main()
