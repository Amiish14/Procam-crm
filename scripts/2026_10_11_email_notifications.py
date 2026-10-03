"""
Email notifications with the original RFQ — schema.

Adds five tables, all new, none of them touching anything that exists:

    lead_raw_emails           the client's original message, kept as a
                              .eml so it can be forwarded as sent
    email_outbox              every outbound email, queued and retried
    notification_prefs        channels, batching and quiet hours, per
                              person
    weekly_pipeline_snapshot  the Friday freeze
    job_leases                which process may run a job right now

Created from the models rather than from hand-written DDL, deliberately:
the migrations in this repository that wrote their own CREATE TABLE
produced tables missing the indexes their models declared, and a later
job had to go and find all forty-three of them. The model is the one
description of the table.

Nothing is backfilled. The originals of emails already received are
fetched by `scripts/backfill_rfq_capture.py`, which is a separate run
because it talks to Microsoft Graph for every lead and wants to be
watched the first time.

Usage
    .venv/bin/python scripts/2026_10_11_email_notifications.py --check
    .venv/bin/python scripts/2026_10_11_email_notifications.py
    .venv/bin/python scripts/2026_10_11_email_notifications.py --down --yes
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from sqlalchemy import inspect                            # noqa: E402

from app import app, db                                   # noqa: E402
from app.models.mailops import (                          # noqa: E402
    EmailOutbox, JobLease, LeadRawEmail, NotificationPref,
    WeeklyPipelineSnapshot)

TABLES = (
    ('lead_raw_emails', LeadRawEmail),
    ('email_outbox', EmailOutbox),
    ('notification_prefs', NotificationPref),
    ('weekly_pipeline_snapshot', WeeklyPipelineSnapshot),
    ('job_leases', JobLease),
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true',
                    help='say what would happen and write nothing')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        from app import Employee
        if not Employee.query.count():
            raise SystemExit('Refusing to run: not the production database.')

        insp = inspect(db.engine)
        present = {name: insp.has_table(name) for name, _ in TABLES}

        if args.down:
            have = [n for n, _ in TABLES if present[n]]
            if args.check or not have:
                print('== DRY-RUN == WOULD drop: '
                      + (', '.join(have) if have else 'nothing'))
                print('  This loses the queue (anything not yet sent is '
                      'gone, and nothing re-queues it), the record of '
                      'which originals were captured — the .eml files '
                      'stay on disk but become unreachable — and every '
                      "person's notification preferences.")
                return
            if not args.yes:
                raise SystemExit('  Refusing without --yes.')
            for name, model in reversed(TABLES):
                if present[name]:
                    model.__table__.drop(bind=db.engine)
                    print(f'  - {name}')
            return

        if args.check:
            print('== DRY-RUN — nothing written ==')
            for name, _ in TABLES:
                print(f'  {"(already there)" if present[name] else "WOULD create"}'
                      f'  {name}')
            print('\n  Nothing is backfilled and no existing table is '
                  'touched. With the FEATURE_* flags unset the new tables '
                  'are written to by nothing, so applying this on its own '
                  'changes no behaviour.')
            return

        db.metadata.create_all(
            bind=db.engine,
            tables=[model.__table__ for name, model in TABLES
                    if not present[name]])
        after = inspect(db.engine)
        for name, _ in TABLES:
            state = 'created' if (not present[name]
                                  and after.has_table(name)) else 'already there'
            print(f'  + {name} — {state}')
        print('\nDone. Turn the behaviour on one flag at a time; see '
              'docs/operations/NOTIFICATIONS_RUNBOOK.md.')


if __name__ == '__main__':
    main()
