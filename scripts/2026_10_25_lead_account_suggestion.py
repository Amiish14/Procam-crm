"""
CRM-ACCOUNT-FIRST-01 — the suggestion table.

    .venv/bin/python scripts/2026_10_25_lead_account_suggestion.py --check
    .venv/bin/python scripts/2026_10_25_lead_account_suggestion.py
    .venv/bin/python scripts/2026_10_25_lead_account_suggestion.py --down --yes

Creates `lead_account_suggestion`. Additive; holds guesses, not
links, so dropping it loses nothing a person decided.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect, text                      # noqa: E402

from app import app, db                                   # noqa: E402
# By name: `import app.models.assignment` rebinds `app` to the
# package and the Flask object disappears.
from app.models.assignment import (LeadAccountSuggestion  # noqa: E402
                                   as _SUG)

TABLE = 'lead_account_suggestion'


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
        here = TABLE in _tables()
        print(f'  {TABLE}: {"present" if here else "absent"}')

        if args.check:
            if here:
                n = db.session.execute(text(
                    f'SELECT COUNT(*) FROM {TABLE}')).scalar()
                acted = db.session.execute(text(
                    f'SELECT COUNT(*) FROM {TABLE} '
                    f'WHERE acted_at IS NOT NULL')).scalar()
                print(f'  rows: {n} ({acted} acted on)')
            print('\nCHECK ONLY — nothing written.')
            return

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            if not here:
                print('  already absent')
                return
            db.session.execute(text(f'DROP TABLE {TABLE}'))
            db.session.commit()
            print(f'  dropped {TABLE} — it held guesses, not links, '
                  f'so no decision is lost')
            return

        if here:
            print('  already present — nothing to do')
            return
        _SUG.__table__.create(db.engine)
        print(f'  created {TABLE}')


if __name__ == '__main__':
    main()
