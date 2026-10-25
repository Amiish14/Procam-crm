"""
CRM-QUOTE-REV-02 §5.4 — the unmatched quote mail queue.

    .venv/bin/python scripts/2026_10_26_unmatched_quote_mail.py --check
    .venv/bin/python scripts/2026_10_26_unmatched_quote_mail.py
    .venv/bin/python scripts/2026_10_26_unmatched_quote_mail.py --down --yes

The spec says to reuse FF-COMMS-01's unmatched queue. Stage A
found there is none: `ff_inbound.link` returns a reason to its
caller and nothing persists it, so every unplaceable vendor reply
since that release has been a log line. This is that queue,
written once.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect, text                      # noqa: E402

from app import app, db                                   # noqa: E402
from app.models.quote import UnmatchedQuoteMail as _Q     # noqa: E402

TABLE = 'unmatched_quote_mail'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        try:
            here = TABLE in set(inspect(db.engine).get_table_names())
        except Exception:                                 # noqa: BLE001
            here = False
        print(f'  {TABLE}: {"present" if here else "absent"}')

        if args.check:
            if here:
                for label, where in (('open', "status = 'open'"),
                                     ('all', '1=1')):
                    n = db.session.execute(text(
                        f'SELECT COUNT(*) FROM {TABLE} '
                        f'WHERE {where}')).scalar()
                    print(f'  {label}: {n}')
            print('\nCHECK ONLY — nothing written.')
            return
        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            if here:
                db.session.execute(text(f'DROP TABLE {TABLE}'))
                db.session.commit()
                print(f'  dropped {TABLE}')
            return
        if here:
            print('  already present — nothing to do')
            return
        _Q.__table__.create(db.engine)
        print(f'  created {TABLE}')


if __name__ == '__main__':
    main()
