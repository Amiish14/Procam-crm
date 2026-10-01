"""
Sales review — schema.

Adds
    review_actions   what a sales review agreed, who owes it, when it is
                     due, and what became of it. Unresolved rows are what
                     the next meeting's "Previous actions" is built from,
                     so nothing else has to remember them.

Additive and empty: no existing table is touched and nothing is
backfilled. There is no history of review actions to invent — a row
written here is something a person typed in a meeting.

`--down` drops the table, which loses every commitment anyone has
recorded, so it refuses without `--yes`.

Usage
    python scripts/2026_10_07_review_actions.py --check
    python scripts/2026_10_07_review_actions.py
    python scripts/2026_10_07_review_actions.py --down --yes
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, '.env'))
except ImportError:
    pass

from sqlalchemy import create_engine, text          # noqa: E402

TABLE = 'review_actions'

DDL = f"""
CREATE TABLE {TABLE} (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    review_scope       VARCHAR(80)  NOT NULL,
    subject_emp_code   VARCHAR(20),
    owner_emp_code     VARCHAR(20),
    due_date           DATE,
    description        VARCHAR(500) NOT NULL,
    linked_entity_type VARCHAR(20),
    linked_entity_id   INTEGER,
    status             VARCHAR(10)  NOT NULL DEFAULT 'open',
    created_by         VARCHAR(20),
    created_at         DATETIME,
    closed_at          DATETIME,
    carried_from_id    INTEGER REFERENCES {TABLE}(id)
)
"""

#: The agenda reads "everything unresolved on this review scope" on every
#: page load, and the command view reads "everything unresolved owed by
#: these people". Both are covered here.
INDEXES = (
    (f'ix_{TABLE}_review_scope', f'{TABLE}(review_scope)'),
    (f'ix_{TABLE}_status', f'{TABLE}(status)'),
    (f'ix_{TABLE}_owner_emp_code', f'{TABLE}(owner_emp_code)'),
    (f'ix_{TABLE}_subject_emp_code', f'{TABLE}(subject_emp_code)'),
    (f'ix_{TABLE}_due_date', f'{TABLE}(due_date)'),
    (f'ix_{TABLE}_created_at', f'{TABLE}(created_at)'),
    (f'ix_{TABLE}_carried_from_id', f'{TABLE}(carried_from_id)'),
)


def _db_url():
    return os.environ.get('DATABASE_URL') or (
        'sqlite:///' + os.path.join(_ROOT, 'procam_crm.db'))


def _tables(conn):
    return {r[0] for r in conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table'"))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    print(f'database: {_db_url()}')
    with create_engine(_db_url()).begin() as conn:
        if not conn.execute(text('SELECT COUNT(*) FROM employees')).scalar():
            raise SystemExit('Refusing to run: not the production database.')
        have = _tables(conn)

        if args.down:
            if TABLE not in have:
                print(f'== DRY-RUN == {TABLE} is not there; nothing to drop.')
                return
            rows = conn.execute(text(f'SELECT COUNT(*) FROM {TABLE}')).scalar()
            if args.check:
                print(f'== DRY-RUN == WOULD drop {TABLE} ({rows} rows)')
                print('  This loses every action a review has agreed.')
                return
            if not args.yes:
                raise SystemExit('  Refusing without --yes.')
            conn.execute(text(f'DROP TABLE {TABLE}'))
            print(f'  - {TABLE} ({rows} rows)')
            return

        if args.check:
            print('== DRY-RUN — nothing written ==')
            print(f'  WOULD create: '
                  f'{TABLE if TABLE not in have else "(already there)"}')
            print(f'  WOULD index:  {len(INDEXES)} columns')
            print('  Nothing backfilled; no existing table is touched.')
            return

        if TABLE not in have:
            conn.execute(text(DDL))
            print(f'  + {TABLE}')
        else:
            print(f'  = {TABLE} already there')
        for name, target in INDEXES:
            conn.execute(text(f'CREATE INDEX IF NOT EXISTS {name} ON {target}'))
        print(f'  + {len(INDEXES)} indexes (if not already there)')
    print('  done.')


if __name__ == '__main__':
    main()
