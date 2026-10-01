"""
Global CRM (Release 4, Group K) — schema.

Adds two tables, and no columns at all:

    contact_relationships   the three facts about a person that the
                            contacts table has never carried — the
                            relationship Procam has with them, a second
                            person looking after them, and which desk
                            owns them.
    contact_assignments     append-only history of who looked after a
                            contact, with the reason each time.

Why tables and not columns on `contacts`
    A model column the database lacks breaks EVERY query on that table,
    not only the new ones — and `contacts` is read by the lead screens,
    the Copilot, the card scanner, Data Quality and the imports. A deploy
    that landed before this script ran would take all of them down at
    once. New tables cannot do that: code that has not been told about
    them carries on unchanged, and Global CRM degrades to "nothing
    recorded yet" until the script runs.

    Because they are new tables, there is nothing to add to the boot
    autoheal column list. `db.create_all()` creates them on a fresh
    database once `app.directory.models` is imported; on an existing
    database, this script is what creates them.

Nothing is backfilled. A contact's vertical already reads through to the
account's vertical and their relationship through to the legacy
`agent_type`, so an empty table is the correct starting state — writing
guesses into it would make a guess look like somebody's decision.

Usage
    python scripts/2026_10_08_contact_relationships.py --check
    python scripts/2026_10_08_contact_relationships.py
    python scripts/2026_10_08_contact_relationships.py --down --yes
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

from sqlalchemy import create_engine, inspect, text      # noqa: E402

TABLES = ('contact_relationships', 'contact_assignments')

#: Written out rather than taken from the model, so this script is
#: readable on its own and runs without importing the application.
DDL = {
    'contact_relationships': """
        CREATE TABLE contact_relationships (
            id                INTEGER PRIMARY KEY,
            contact_id        INTEGER NOT NULL UNIQUE
                                  REFERENCES contacts (id),
            relationship_type VARCHAR(40),
            secondary_pic     VARCHAR(20),
            vertical          VARCHAR(80),
            updated_at        TIMESTAMP,
            updated_by        VARCHAR(20)
        )""",
    'contact_assignments': """
        CREATE TABLE contact_assignments (
            id                INTEGER PRIMARY KEY,
            contact_id        INTEGER NOT NULL REFERENCES contacts (id),
            previous_pic_code VARCHAR(20),
            new_pic_code      VARCHAR(20),
            assigned_by       VARCHAR(20) NOT NULL,
            reason            TEXT,
            changes           JSON,
            assigned_at       TIMESTAMP
        )""",
}

INDEXES = (
    ('ix_contact_relationships_contact_id', 'contact_relationships',
     'contact_id'),
    ('ix_contact_relationships_vertical', 'contact_relationships', 'vertical'),
    ('ix_contact_relationships_relationship_type', 'contact_relationships',
     'relationship_type'),
    ('ix_contact_relationships_secondary_pic', 'contact_relationships',
     'secondary_pic'),
    ('ix_contact_assignments_contact_id', 'contact_assignments', 'contact_id'),
    ('ix_contact_assignments_assigned_at', 'contact_assignments',
     'assigned_at'),
)


def _db_url():
    return os.environ.get('DATABASE_URL') or (
        'sqlite:///' + os.path.join(_ROOT, 'procam_crm.db'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    url = _db_url()
    print(f'database: {url}')
    engine = create_engine(url)
    # Postgres spells the serial primary key differently; SQLite is what
    # production runs, and create_all covers the rest.
    pg = engine.dialect.name not in ('sqlite',)
    with engine.begin() as conn:
        if not conn.execute(text('SELECT COUNT(*) FROM employees')).scalar():
            raise SystemExit('Refusing to run: not the production database.')
        have = set(inspect(conn).get_table_names())

        if args.down:
            present = [t for t in TABLES if t in have]
            if args.check or not present:
                print(f'== DRY-RUN == WOULD drop {present or "nothing"}')
                print('  This loses every contact assignment history row and '
                      'every relationship type anyone has set.')
                return
            if not args.yes:
                raise SystemExit('  Refusing without --yes.')
            for t in present:
                conn.execute(text(f'DROP TABLE {t}'))
                print(f'  - {t}')
            return

        missing = [t for t in TABLES if t not in have]
        if args.check:
            print('== DRY-RUN — nothing written ==')
            print(f'  WOULD create: {missing or "(already there)"}')
            print('  Nothing backfilled.')
            return
        for t in missing:
            ddl = DDL[t]
            if pg:                                      # pragma: no cover
                ddl = ddl.replace('INTEGER PRIMARY KEY', 'SERIAL PRIMARY KEY')
            conn.execute(text(ddl))
            print(f'  + {t}')
        for name, table, col in INDEXES:
            if table in TABLES:
                conn.execute(text(f'CREATE INDEX IF NOT EXISTS {name} '
                                  f'ON {table} ({col})'))
    print('  done.')


if __name__ == '__main__':
    main()
