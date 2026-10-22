"""
CRM-FORWARD-01 — who forwarded it, and what kind of enquiry it is.

    .venv/bin/python scripts/2026_10_22_forward_capture.py --check
    .venv/bin/python scripts/2026_10_22_forward_capture.py
    .venv/bin/python scripts/2026_10_22_forward_capture.py --down --yes

  leads.forwarded_by_email / _name / forwarded_subject
  leads.lead_type / lead_platform
  leads.bid_outcome / bid_reason

Additive, and nothing is backfilled. The forwarder was never
stored and no raw .eml survives for the 226 SuperProcure
postings, so there is nothing to recover it from — a guess would
name the wrong colleague, which is worse than an empty field.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect, text                      # noqa: E402

from app import app, db                                   # noqa: E402

COLUMNS = (
    ('leads', 'forwarded_by_email', 'VARCHAR(320)'),
    ('leads', 'forwarded_by_name', 'VARCHAR(160)'),
    ('leads', 'forwarded_subject', 'VARCHAR(500)'),
    ('leads', 'lead_type', 'VARCHAR(32)'),
    ('leads', 'lead_platform', 'VARCHAR(40)'),
    ('leads', 'bid_outcome', 'VARCHAR(16)'),
    ('leads', 'bid_reason', 'VARCHAR(300)'),
)


def _cols(table):
    try:
        return {c['name'] for c in inspect(db.engine).get_columns(table)}
    except Exception:
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
        print(f'  {len(COLUMNS) - len(absent)} present, '
              f'{len(absent)} absent')
        for t, c in absent:
            print(f'    absent: {t}.{c}')

        if args.check:
            if 'lead_type' in _cols('leads'):
                for label, sql in (
                        ('auction leads',
                         "SELECT COUNT(*) FROM leads "
                         "WHERE lead_type = 'auction'"),
                        ('with a forwarder recorded',
                         'SELECT COUNT(*) FROM leads '
                         'WHERE forwarded_by_email IS NOT NULL')):
                    n = db.session.execute(text(sql)).scalar()
                    print(f'  {label}: {n}')
            print('\nCHECK ONLY — nothing written.')
            return

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            for tbl, col, _d in reversed(COLUMNS):
                if col not in _cols(tbl):
                    continue
                try:
                    db.session.execute(text(
                        f'ALTER TABLE {tbl} DROP COLUMN {col}'))
                    db.session.commit()
                    print(f'  dropped {tbl}.{col}')
                except Exception as exc:              # noqa: BLE001
                    db.session.rollback()
                    print(f'  !! {tbl}.{col}: {str(exc)[:120]}')
            return

        for tbl, col, ddl in COLUMNS:
            if col in _cols(tbl):
                continue
            db.session.execute(text(
                f'ALTER TABLE {tbl} ADD COLUMN {col} {ddl}'))
            db.session.commit()
            print(f'  added {tbl}.{col}')
        print('\n  Done. Nothing backfilled: the forwarder was '
              'never stored, and no raw .eml survives to recover '
              'it from.')


if __name__ == '__main__':
    main()
