"""
Intake screens — the two columns the review queue needs.

email_classifications recorded what was decided but not enough of the
message to review it. A queue that can only show a subject line cannot
be acted on, and "Accept as lead" needs the body to build the lead from.

Adds
    email_classifications.payload       JSON — sender, body, attachments
    email_classifications.review_state  pending | accepted | rejected |
                                        reclassified

Existing rows are marked 'accepted' where they created a lead and
'pending' where they did not, so the queue opens with the backlog in it
rather than empty.

Usage
    python scripts/2026_09_27_intake_screens.py --check
    python scripts/2026_09_27_intake_screens.py
    python scripts/2026_09_27_intake_screens.py --down --yes
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

COLUMNS = [('payload', 'JSON'), ('review_state', 'VARCHAR(16)')]


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


def _cols(conn, table):
    return {r[1] for r in conn.execute(text(f'PRAGMA table_info({table})'))}


def _has_table(conn, name):
    return bool(conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"),
        {'n': name}).fetchone())


def up(conn, dry):
    if not _has_table(conn, 'email_classifications'):
        raise SystemExit(
            'Refusing to run: email_classifications does not exist. Run '
            'scripts/2026_09_26_lead_intake_engine.py first.')
    have = _cols(conn, 'email_classifications')
    todo = [(c, t) for c, t in COLUMNS if c not in have]
    existing = conn.execute(text(
        'SELECT COUNT(*) FROM email_classifications')).scalar() or 0

    if dry:
        print('== DRY-RUN — nothing written ==')
        print(f'  WOULD add: '
              f'{", ".join(c for c, _ in todo) or "(none — already there)"}')
        print(f'  WOULD backfill review_state on {existing} existing row(s)')
        return

    for col, coltype in todo:
        conn.execute(text(
            f'ALTER TABLE email_classifications ADD COLUMN {col} {coltype}'))
        print(f'  + email_classifications.{col}')
    conn.execute(text('CREATE INDEX IF NOT EXISTS '
                      'ix_email_classifications_review_state '
                      'ON email_classifications (review_state)'))

    # A decision that produced a lead was, in effect, already accepted.
    # Everything else is waiting for someone to look at it.
    n1 = conn.execute(text(
        "UPDATE email_classifications SET review_state = 'accepted' "
        'WHERE review_state IS NULL AND created_lead_id IS NOT NULL')).rowcount
    n2 = conn.execute(text(
        "UPDATE email_classifications SET review_state = 'pending' "
        'WHERE review_state IS NULL')).rowcount
    print(f'  marked {n1} accepted, {n2} pending')


def down(conn, dry, confirmed):
    have = _cols(conn, 'email_classifications')
    present = [c for c, _ in COLUMNS if c in have]
    print('  This DISCARDS the stored message payloads and every review '
          'decision.')
    if dry:
        print('== DRY-RUN — nothing written ==')
        print(f'  WOULD drop: {", ".join(present) or "(none)"}')
        return
    if not confirmed:
        raise SystemExit('\n  Refusing to drop data without --yes.')
    conn.execute(text('DROP INDEX IF EXISTS '
                      'ix_email_classifications_review_state'))
    for col in present:
        conn.execute(text(
            f'ALTER TABLE email_classifications DROP COLUMN {col}'))
        print(f'  - email_classifications.{col}')


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
        if args.down:
            down(conn, args.check, args.yes)
        else:
            up(conn, args.check)
    print('  done.' if not args.check else '== end dry-run ==')


if __name__ == '__main__':
    main()
