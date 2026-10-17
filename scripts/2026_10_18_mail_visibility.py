"""
Mail visibility: what happened to the mail you forwarded.

Columns and tables, all additive. No row is written, moved or deleted.

    notifications.content_hash        tell a repeat from a different
                                      update about the same lead
    email_outbox.render_json          rebuild a delayed message from
                                      the record as it stands now
    mail_ingest_log.forwarded_by      who forwarded it in
    mail_ingest_log.forwarded_by_emp  ... as an employee code
    mail_ingest_log.lead_stage_at_ingest
                                      what the lead was when the mail
                                      landed, so "it went to a Lost
                                      lead" is answerable afterwards

Nothing is backfilled that cannot be derived safely. `forwarded_by` is
filled from the stored raw mail where there is one, and left null
where there is not — a guessed forwarder would put somebody else's
mail on a person's My Mail page.

    .venv/bin/python scripts/2026_10_18_mail_visibility.py --check
    .venv/bin/python scripts/2026_10_18_mail_visibility.py
    .venv/bin/python scripts/2026_10_18_mail_visibility.py --backfill
    .venv/bin/python scripts/2026_10_18_mail_visibility.py --down --yes
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect, text                      # noqa: E402

from app import Employee, app, db                         # noqa: E402

COLUMNS = (
    ('notifications', 'content_hash', 'VARCHAR(32)'),
    ('email_outbox', 'render_json', 'TEXT'),
    ('mail_ingest_log', 'forwarded_by', 'VARCHAR(200)'),
    ('mail_ingest_log', 'forwarded_by_emp', 'VARCHAR(20)'),
    ('mail_ingest_log', 'lead_stage_at_ingest', 'VARCHAR(40)'),
)

INDEXES = (
    ('ix_notifications_content_hash', 'notifications', 'content_hash'),
    ('ix_mail_ingest_log_forwarded_by_emp', 'mail_ingest_log',
     'forwarded_by_emp'),
)


def _has(table, column):
    try:
        return column in {c['name'] for c in
                          inspect(db.engine).get_columns(table)}
    except Exception:
        return False


def _apply(dry):
    insp = inspect(db.engine)
    for table, column, ddl in COLUMNS:
        if not insp.has_table(table):
            print(f'  . {table} does not exist — skipping {column}')
            continue
        if _has(table, column):
            print(f'  . {table}.{column} already there')
            continue
        if dry:
            print(f'  WOULD add {table}.{column} {ddl}')
            continue
        db.session.execute(text(
            f'ALTER TABLE {table} ADD COLUMN {column} {ddl}'))
        db.session.commit()
        print(f'  + {table}.{column}')

    for name, table, column in INDEXES:
        if not insp.has_table(table) or not _has(table, column):
            continue
        if dry:
            print(f'  WOULD create index {name}')
            continue
        try:
            db.session.execute(text(
                f'CREATE INDEX IF NOT EXISTS {name} ON {table} ({column})'))
            db.session.commit()
            print(f'  + index {name}')
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'  ! index {name}: {str(exc)[:100]}')


def _backfill(dry):
    """Fill forwarded_by from the stored raw mail, where there is one."""
    from app.models.ingest_log import MailIngestLog

    rows = (MailIngestLog.query
            .filter(MailIngestLog.forwarded_by.is_(None))
            .order_by(MailIngestLog.id.desc()).limit(5000).all())
    print(f'  {len(rows)} ingest rows with no forwarder recorded')

    by_email = {}
    for e in Employee.query.all():
        addr = (e.email or '').strip().lower()
        if addr:
            by_email[addr] = e.emp_code

    filled, matched = 0, 0
    for row in rows:
        sender = (row.from_addr or '').strip().lower()
        if '<' in sender and '>' in sender:
            sender = sender[sender.rfind('<') + 1:sender.rfind('>')].strip()
        if not sender or '@' not in sender:
            continue
        domain = sender.rsplit('@', 1)[1]
        # Only an internal sender is a forwarder. An external address
        # is the client, and putting the client on somebody's My Mail
        # page as the forwarder would be wrong and confusing.
        if not domain.endswith(('procamgroup.in', 'procamlogistics.com')):
            continue
        filled += 1
        code = by_email.get(sender)
        if code:
            matched += 1
        if not dry:
            row.forwarded_by = sender[:200]
            row.forwarded_by_emp = code
    if dry:
        print(f'  WOULD fill {filled} forwarder(s), {matched} matched to an '
              f'employee code')
        return
    db.session.commit()
    print(f'  + {filled} forwarder(s) filled, {matched} matched to an '
          f'employee code')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--backfill', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        if not Employee.query.count():
            raise SystemExit('Refusing to run: not the production database.')

        if args.down:
            print('== nothing to drop ==')
            print('  Every change here is an added column. SQLite cannot '
                  'drop a column without rewriting the table, and these '
                  'five are read with COALESCE everywhere, so leaving '
                  'them costs nothing and removing them risks the table.')
            print('  To roll the behaviour back, revert the code and '
                  'restart; the columns are then simply unread.')
            return

        if args.backfill:
            _backfill(dry=args.check)
            return

        _apply(dry=args.check)
        if args.check:
            print('\n  Then, separately:')
            print('    scripts/2026_10_18_mail_visibility.py --backfill '
                  '--check')
            return
        print('\nDone. Restart the service, then run --backfill.')


if __name__ == '__main__':
    main()
