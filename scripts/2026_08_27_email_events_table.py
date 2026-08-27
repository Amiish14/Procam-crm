"""
v2026-08 — create email_events table (mailbox-mode visibility log).

Additive-only, idempotent.

Usage:
    python scripts/2026_08_27_email_events_table.py           # dry-run
    python scripts/2026_08_27_email_events_table.py --apply   # commit
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import inspect
from app import app, db, EmailEvent


def main(apply_changes: bool):
    with app.app_context():
        insp = inspect(db.engine)
        if insp.has_table('email_events'):
            print('  = email_events table already exists')
            return
        if apply_changes:
            EmailEvent.__table__.create(bind=db.engine)
            print('  [OK] email_events created')
        else:
            print('  + Would CREATE TABLE email_events')
            print('\nDRY RUN — re-run with --apply.')


if __name__ == '__main__':
    main(apply_changes='--apply' in sys.argv)
