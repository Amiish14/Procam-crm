"""
The webhook outbox — events the TMS is owed.

One table. `notify_tms` previously posted inline and logged the
failure, which loses the event when the TMS is down: a `lead.won`
that never arrived is indistinguishable, from the TMS's side, from a
deal that was always won. Nothing could discover it afterwards.

    webhook_outbox   one row per CRM event owed to the TMS, with the
                     event id that stays the same across retries

Nothing is backfilled. Events that were dropped before this existed
are in `integration_log` with status 'error' and can be identified
from there, but they are not replayed automatically — replaying a
three-week-old "deal won" into a system that may have learnt about it
another way is a decision, not a migration.

    .venv/bin/python scripts/2026_10_14_webhook_outbox.py --check
    .venv/bin/python scripts/2026_10_14_webhook_outbox.py
    .venv/bin/python scripts/2026_10_14_webhook_outbox.py --down --yes
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect                            # noqa: E402

from app import app, db                                   # noqa: E402
from app.models.integration import WebhookOutbox          # noqa: E402

TABLE = 'webhook_outbox'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        from app import Employee
        if not Employee.query.count():
            raise SystemExit('Refusing to run: not the production database.')
        exists = inspect(db.engine).has_table(TABLE)

        if args.down:
            if args.check or not exists:
                print(f'== DRY-RUN == WOULD drop '
                      f'{TABLE if exists else "nothing"}')
                print('  Anything queued and not yet delivered is lost, '
                      'and the TMS has no way to discover it. Deliver '
                      'the queue first:\n'
                      '    .venv/bin/python scripts/webhook_worker.py '
                      '--status')
                return
            if not args.yes:
                raise SystemExit('  Refusing without --yes.')
            WebhookOutbox.__table__.drop(bind=db.engine)
            print(f'  - {TABLE}')
            return

        if args.check:
            print('== DRY-RUN — nothing written ==')
            print(f'  {"(already there)" if exists else "WOULD create"}  '
                  f'{TABLE}')
            print('\n  Nothing is backfilled. With TMS_WEBHOOK_URL unset '
                  'the CRM queues events and delivers none of them — '
                  'they wait rather than being lost, which is the point '
                  'of the table.')
            return

        if not exists:
            db.metadata.create_all(bind=db.engine,
                                   tables=[WebhookOutbox.__table__])
        print(f'  + {TABLE}')
        print('\nDone. Install procam-crm-webhook.timer to deliver them.')


if __name__ == '__main__':
    main()
