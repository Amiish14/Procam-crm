"""
CRM-RFQ-CHAIN-01 (R2) — the chain's columns and its one new table.

    .venv/bin/python scripts/2026_10_26_rfq_chain.py --check
    .venv/bin/python scripts/2026_10_26_rfq_chain.py
    .venv/bin/python scripts/2026_10_26_rfq_chain.py --down --yes

Additive. Every column is nullable or defaulted, so the 3,693
existing task instances read exactly as they did: `step_no` null
means "not part of a chain", which is true of all of them.

Nothing is backfilled. There are 0 RFQs in production, so there
is no chain to reconstruct — and inventing step timestamps for
work that happened on a spreadsheet would put made-up numbers in
the one report this release exists to make trustworthy.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect, text                      # noqa: E402

from app import app, db                                   # noqa: E402
from app.models.rfq import RfqCondition as _COND          # noqa: E402

COLUMNS = (
    ('task_instances', 'step_no', 'SMALLINT'),
    ('task_instances', 'paused_seconds', 'INTEGER DEFAULT 0'),
    ('task_instances', 'held_at', 'DATETIME'),
    ('task_instances', 'held_by', 'VARCHAR(20)'),
    ('task_instances', 'hold_reason', 'VARCHAR(120)'),
    ('task_instances', 'hold_remarks', 'VARCHAR(400)'),
    ('task_instances', 'result', 'VARCHAR(12)'),
    ('task_instances', 'delay_reason', 'VARCHAR(120)'),
    ('task_instances', 'good_practice_reason', 'VARCHAR(120)'),
    ('task_instances', 'reason_remarks', 'VARCHAR(400)'),
    ('task_instances', 'waiting_for_customer', 'BOOLEAN'),
    ('rfqs', 'priority', 'VARCHAR(16)'),
    ('rfqs', 'customer_deadline', 'DATE'),
    ('rfqs', 'needs_technical', 'BOOLEAN'),
    ('rfqs', 'needs_survey', 'BOOLEAN'),
    ('rfqs', 'registered_at', 'DATETIME'),
    ('rfqs', 'registered_by', 'VARCHAR(20)'),
    ('rfqs', 'held_at', 'DATETIME'),
    ('rfqs', 'held_by', 'VARCHAR(20)'),
    ('rfqs', 'hold_reason', 'VARCHAR(120)'),
    ('rfqs', 'hold_remarks', 'VARCHAR(400)'),
)
TABLE = 'rfq_conditions'


def _cols(table):
    try:
        return {c['name'] for c in inspect(db.engine).get_columns(table)}
    except Exception:                                     # noqa: BLE001
        return set()


def _tables():
    try:
        return set(inspect(db.engine).get_table_names())
    except Exception:                                     # noqa: BLE001
        return set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        absent = [(t, c) for t, c, _d in COLUMNS if c not in _cols(t)]
        here = TABLE in _tables()
        print(f'  columns: {len(COLUMNS) - len(absent)} present, '
              f'{len(absent)} absent')
        print(f'  {TABLE}: {"present" if here else "absent"}')
        for t, c in absent:
            print(f'    absent: {t}.{c}')

        if args.check:
            for label, sql in (
                    ('RFQs today', 'SELECT COUNT(*) FROM rfqs'),
                    ('task instances today',
                     'SELECT COUNT(*) FROM task_instances'),
                    ('already on a chain step',
                     'SELECT COUNT(*) FROM task_instances '
                     'WHERE step_no IS NOT NULL')):
                try:
                    print(f'  {label}: '
                          f'{db.session.execute(text(sql)).scalar()}')
                except Exception as exc:                  # noqa: BLE001
                    print(f'  {label}: unreadable ({str(exc)[:50]})')
            print('\nCHECK ONLY — nothing written.')
            return

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            if here:
                db.session.execute(text(f'DROP TABLE {TABLE}'))
                db.session.commit()
                print(f'  dropped {TABLE}')
            for tbl, col, _d in reversed(COLUMNS):
                if col not in _cols(tbl):
                    continue
                try:
                    db.session.execute(text(
                        f'ALTER TABLE {tbl} DROP COLUMN {col}'))
                    db.session.commit()
                    print(f'  dropped {tbl}.{col}')
                except Exception as exc:                  # noqa: BLE001
                    db.session.rollback()
                    print(f'  !! {tbl}.{col}: {str(exc)[:110]}')
            return

        for tbl, col, ddl in COLUMNS:
            if col in _cols(tbl):
                continue
            db.session.execute(text(
                f'ALTER TABLE {tbl} ADD COLUMN {col} {ddl}'))
            db.session.commit()
            print(f'  added {tbl}.{col}')
        if not here:
            _COND.__table__.create(db.engine)
            print(f'  created {TABLE}')
        print('\n  Done. Nothing backfilled: there is no chain to '
              'reconstruct, and invented step times would poison the '
              'one report this release exists to make trustworthy.')


if __name__ == '__main__':
    main()
