"""
Lead dates, the ingestion log, and the seam to the TMS.

Columns on `leads`
    received_at      when the message reached leads@procamgroup.in.
                     Backfilled from created_at, which is what the
                     ingest has always written, so existing leads keep
                     the date they already show.
    client_sent_at   the customer's own sent time. **Not backfilled** —
                     it was never captured, and inventing it from
                     created_at would assert that every historical
                     forward was sent the day it was relayed, which is
                     exactly the wrong thing to claim.
    needs_review     a lead the ingest could not attribute to a client.

Tables
    mail_ingest_log  one row per message the mailbox saw
    crm_tms_links    which CRM lead is which TMS project
    integration_log  every request in either direction
    app_settings     admin-editable values that are not vocabulary

Usage
    .venv/bin/python scripts/2026_10_13_ingest_and_integration.py --check
    .venv/bin/python scripts/2026_10_13_ingest_and_integration.py
    .venv/bin/python scripts/2026_10_13_ingest_and_integration.py --down --yes
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect, text                      # noqa: E402

from app import app, db                                   # noqa: E402
from app.models.ingest_log import MailIngestLog           # noqa: E402
from app.models.integration import (                      # noqa: E402
    AppSetting, CrmTmsLink, IntegrationLog)

TABLES = (
    ('mail_ingest_log', MailIngestLog),
    ('crm_tms_links', CrmTmsLink),
    ('integration_log', IntegrationLog),
    ('app_settings', AppSetting),
)

COLUMNS = (
    ('leads', 'received_at', 'TIMESTAMP'),
    ('leads', 'client_sent_at', 'TIMESTAMP'),
    ('leads', 'needs_review', 'BOOLEAN'),
)

INDEXES = (
    ('ix_leads_received_at', 'leads', 'received_at'),
    ('ix_leads_needs_review', 'leads', 'needs_review'),
)


def _existing_columns(table):
    return {c['name'] for c in inspect(db.engine).get_columns(table)}


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

        insp = inspect(db.engine)
        have_table = {name: insp.has_table(name) for name, _ in TABLES}
        lead_cols = _existing_columns('leads')
        missing_cols = [(t, c, d) for t, c, d in COLUMNS
                        if c not in lead_cols]

        if args.down:
            drop = [n for n, _ in TABLES if have_table[n]]
            if args.check or not drop:
                print('== DRY-RUN == WOULD drop: '
                      + (', '.join(drop) if drop else 'nothing'))
                print('  The three lead columns are LEFT IN PLACE. SQLite '
                      'cannot drop a column without rebuilding the table, '
                      'and rebuilding a ten-thousand-row leads table to '
                      'remove three nullable columns nothing reads is a '
                      'worse risk than leaving them.')
                print('  Dropping mail_ingest_log loses the record of '
                      'which emails were skipped and why — the leads '
                      'themselves are untouched.')
                return
            if not args.yes:
                raise SystemExit('  Refusing without --yes.')
            for name, model in reversed(TABLES):
                if have_table[name]:
                    model.__table__.drop(bind=db.engine)
                    print(f'  - {name}')
            return

        if args.check:
            print('== DRY-RUN — nothing written ==')
            for name, _ in TABLES:
                print(f'  {"(already there)" if have_table[name] else "WOULD create"}'
                      f'  {name}')
            for table, col, dtype in COLUMNS:
                state = ('(already there)' if col not in
                         {c for _t, c, _d in missing_cols} else 'WOULD add')
                print(f'  {state}  {table}.{col} {dtype}')
            if 'received_at' in {c for _t, c, _d in missing_cols}:
                total = db.session.execute(
                    text('SELECT COUNT(*) FROM leads')).scalar()
                print(f'\n  WOULD backfill received_at = created_at for '
                      f'{total:,} lead(s), so every existing lead keeps '
                      f'the date it already shows.')
                print('  client_sent_at is NOT backfilled: it was never '
                      'captured, and copying created_at into it would '
                      'claim every historical forward was written the day '
                      'it was relayed.')
            return

        for table, col, dtype in missing_cols:
            db.session.execute(
                text(f'ALTER TABLE {table} ADD COLUMN {col} {dtype}'))
            print(f'  + {table}.{col}')
        if missing_cols:
            db.session.commit()

        if ('leads', 'received_at', 'TIMESTAMP') in missing_cols:
            done = db.session.execute(text(
                'UPDATE leads SET received_at = created_at '
                ' WHERE received_at IS NULL')).rowcount
            db.session.commit()
            print(f'  ~ received_at backfilled on {done:,} lead(s)')

        db.metadata.create_all(
            bind=db.engine,
            tables=[model.__table__ for name, model in TABLES
                    if not have_table[name]])
        for name, _ in TABLES:
            print(f'  + {name}')

        for index, table, column in INDEXES:
            try:
                db.session.execute(text(
                    f'CREATE INDEX IF NOT EXISTS {index} '
                    f'ON {table} ({column})'))
            except Exception as exc:                      # noqa: BLE001
                print(f'  ! {index}: {exc}')
        db.session.commit()
        print('\nDone. The ingestion log is at /CRM/admin/mail-ingest.')


if __name__ == '__main__':
    main()
