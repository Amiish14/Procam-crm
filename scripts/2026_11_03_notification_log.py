"""
CRM-USER-ISSUES-02NOV §3 — the table that records every notification.

    .venv/bin/python scripts/2026_11_03_notification_log.py --check
    .venv/bin/python scripts/2026_11_03_notification_log.py
    .venv/bin/python scripts/2026_11_03_notification_log.py --down --yes

  notification_attempts     one row per attempt to tell one person one
                            thing, whatever the outcome — including the
                            refusals, which is the point

Additive. One new table, created from the model. No existing table is
altered and no existing row is touched, so there is nothing to undo on
the way in.

WHY A NEW TABLE RATHER THAN A COLUMN ON email_outbox

Because the rows §3 is about never became outbox rows. A notification
refused for having no address, or for an address `mail_policy` will not
write to, or because the employee has left, produces nothing in
`email_outbox` at all — and a message missing from a log of sent mail
is indistinguishable from a message nobody was ever owed. That
indistinguishability *is* the reported bug.

The two tables are deliberately linked rather than merged:
`notification_attempts.outbox_id` points at the queue row where one was
created. This table answers "did the CRM get as far as trying?"; the
outbox answers "what did the mail server say?".

--down drops the table and with it the history of what failed. The
notifications themselves are unaffected: nothing reads this table to
decide whether to send.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect, text                      # noqa: E402

from app import app, db                                   # noqa: E402

TABLES = ('notification_attempts',)


def _tables():
    try:
        return set(inspect(db.engine).get_table_names())
    except Exception:
        return set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    import app.models.notification_log as _models         # noqa: F401

    with app.app_context():
        print(f'database: {db.engine.url}')
        here = _tables()
        for name in TABLES:
            print(f'  {name}: '
                  + ('present' if name in here else 'absent'))

        if args.check:
            if 'notification_attempts' in here:
                rows = db.session.execute(text(
                    'SELECT status, COUNT(*) FROM notification_attempts '
                    'GROUP BY status')).all()
                total = sum(n for _s, n in rows)
                print(f'\n  attempts recorded: {total}')
                for status, count in sorted(rows, key=lambda r: -r[1]):
                    print(f'    {status:<12} {count}')
                if not rows:
                    print('    (none yet — nothing has been notified '
                          'since the table was created)')
            print('\nCHECK ONLY — nothing written.')
            return

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            for name in reversed(TABLES):
                if name not in _tables():
                    print(f'  {name} already absent.')
                    continue
                try:
                    db.session.execute(text(f'DROP TABLE {name}'))
                    db.session.commit()
                    print(f'  dropped {name}')
                except Exception as exc:              # noqa: BLE001
                    db.session.rollback()
                    print(f'  !! {name}: {str(exc)[:120]}')
            return

        db.create_all()
        after = _tables()
        missing = [n for n in TABLES if n not in after]
        for name in TABLES:
            print(f'  {name}: '
                  + ('present' if name in after else 'STILL ABSENT'))
        if missing:
            raise SystemExit(
                f'FAILED — {", ".join(missing)} was not created. The '
                f'model may not be on the metadata; check that app.py '
                f'imports app.models.notification_log.')

        # The column list, read back from the database rather than from
        # the model, because "the table exists" and "the table has the
        # columns the code writes" are different claims.
        cols = {c['name'] for c
                in inspect(db.engine).get_columns('notification_attempts')}
        wanted = {'id', 'created_at', 'user_code', 'address', 'kind',
                  'event_key', 'entity_type', 'entity_id', 'title',
                  'status', 'error', 'outbox_id', 'attempts',
                  'retried_by', 'retried_at'}
        short = wanted - cols
        if short:
            raise SystemExit(
                f'FAILED — notification_attempts is missing '
                f'{", ".join(sorted(short))}.')
        print(f'  columns: {len(cols)} present, all {len(wanted)} '
              f'the code writes')
        print('\nVERIFIED')


if __name__ == '__main__':
    main()
