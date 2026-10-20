"""
FF-LINK-01 part 5 — the other names a client trades under.

    .venv/bin/python scripts/2026_10_20_company_aliases.py --check
    .venv/bin/python scripts/2026_10_20_company_aliases.py
    .venv/bin/python scripts/2026_10_20_company_aliases.py --down --yes

  companies.aliases                     JSON list of other names

One column, additive, nothing backfilled here — the names are
written by `scripts/ff_account_hygiene.py --apply-aliases`, which
takes them from mail signatures and records an audit row each.

Why it matters: `ff_privacy._alias_set` already reads
`account.aliases`, so the column is the only thing standing between
the masking and the accounts it cannot currently protect. An account
named "Efl" has two letters of signal and `variants()` returns
nothing for it, so a rate request to a carrier carries the client's
real name in the body while the masking reports success. With the
alias "Expo Freight Private Limited" present, that string is
redacted like any other secret.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect, text                      # noqa: E402

from app import app, db                                   # noqa: E402

COLUMNS = (
    ('companies', 'aliases', 'JSON'),
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
        for tbl, col, _d in COLUMNS:
            print(f'  {tbl}.{col}: '
                  + ('present' if col in _cols(tbl) else 'absent'))

        if args.check:
            if 'aliases' in _cols('companies'):
                n = db.session.execute(text(
                    'SELECT COUNT(*) FROM companies '
                    "WHERE aliases IS NOT NULL AND aliases <> '[]'"
                )).scalar()
                print(f'\n  companies carrying an alias: {n}')
            print('\nCHECK ONLY — nothing written.')
            return

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            for tbl, col, _d in reversed(COLUMNS):
                if col not in _cols(tbl):
                    print(f'  {tbl}.{col} already absent.')
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
                print(f'  {tbl}.{col} already present.')
                continue
            db.session.execute(text(
                f'ALTER TABLE {tbl} ADD COLUMN {col} {ddl}'))
            db.session.commit()
            print(f'  added {tbl}.{col}')


if __name__ == '__main__':
    main()
