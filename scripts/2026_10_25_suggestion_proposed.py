"""
Two columns so a suggestion can say "no account, but this address".

    .venv/bin/python scripts/2026_10_25_suggestion_proposed.py --check
    .venv/bin/python scripts/2026_10_25_suggestion_proposed.py
    .venv/bin/python scripts/2026_10_25_suggestion_proposed.py --down --yes

Outbound mail carries the customer in its To: line. Without
somewhere to record that, Account check shows "nothing matched"
for a lead whose customer we have just read off the header, and
the person has to find it again by hand.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect, text                      # noqa: E402

from app import app, db                                   # noqa: E402

TABLE = 'lead_account_suggestion'
COLUMNS = (('proposed_domain', 'VARCHAR(200)'),
           ('proposed_name', 'VARCHAR(200)'))


def _cols():
    try:
        return {c['name'] for c in inspect(db.engine).get_columns(TABLE)}
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
        have = _cols()
        absent = [c for c, _d in COLUMNS if c not in have]
        print(f'  {len(COLUMNS) - len(absent)} present, '
              f'{len(absent)} absent')

        if args.check:
            print('\nCHECK ONLY — nothing written.')
            return
        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            for col, _d in reversed(COLUMNS):
                if col not in _cols():
                    continue
                try:
                    db.session.execute(text(
                        f'ALTER TABLE {TABLE} DROP COLUMN {col}'))
                    db.session.commit()
                    print(f'  dropped {col}')
                except Exception as exc:                  # noqa: BLE001
                    db.session.rollback()
                    print(f'  !! {col}: {str(exc)[:110]}')
            return

        for col, ddl in COLUMNS:
            if col in _cols():
                continue
            db.session.execute(text(
                f'ALTER TABLE {TABLE} ADD COLUMN {col} {ddl}'))
            db.session.commit()
            print(f'  added {col}')
        print('\n  Done.')


if __name__ == '__main__':
    main()
