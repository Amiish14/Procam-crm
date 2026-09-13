"""
Procam AI's audit trail — §11.

Adds one table. Nothing existing is touched, and nothing is backfilled:
the log starts when the Copilot does.

    copilot_log   one row per question asked

The table carries the question, the intent that answered it, the data
scope in force, which sources were read, whether it answered, how long
it took, and the thumbs-up/down with its reason. It deliberately does
not carry the answer's rows — that would copy business data into a log
with different retention — nor any model reasoning, which §11 excludes.

Usage
    python scripts/2026_09_30_copilot_log.py --check
    python scripts/2026_09_30_copilot_log.py
    python scripts/2026_09_30_copilot_log.py --down --yes
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


def _db_url():
    return os.environ.get('DATABASE_URL') or (
        'sqlite:///' + os.path.join(_ROOT, 'procam_crm.db'))


def _guard(conn):
    try:
        n = conn.execute(text('SELECT COUNT(*) FROM employees')).scalar()
    except Exception:
        n = 0
    if not n:
        raise SystemExit('Refusing to run: not the production database.')
    return n


DDL = """
CREATE TABLE IF NOT EXISTS copilot_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    emp_code         VARCHAR(20),
    question         VARCHAR(1000) NOT NULL,
    intent           VARCHAR(60),
    data_scope       VARCHAR(12),
    sources          VARCHAR(500),
    answered         INTEGER DEFAULT 0,
    restricted       INTEGER DEFAULT 0,
    model_used       INTEGER DEFAULT 0,
    latency_ms       INTEGER,
    created_at       DATETIME,
    pinned           INTEGER DEFAULT 0,
    pinned_at        DATETIME,
    helpful          INTEGER,
    feedback_reason  VARCHAR(60),
    feedback_note    VARCHAR(500),
    feedback_by      VARCHAR(20)
)
"""

INDEXES = (
    'CREATE INDEX IF NOT EXISTS ix_copilot_log_emp ON copilot_log (emp_code)',
    'CREATE INDEX IF NOT EXISTS ix_copilot_log_intent ON copilot_log (intent)',
    'CREATE INDEX IF NOT EXISTS ix_copilot_log_created ON copilot_log (created_at)',
    'CREATE INDEX IF NOT EXISTS ix_copilot_log_answered ON copilot_log (answered)',
    'CREATE INDEX IF NOT EXISTS ix_copilot_log_helpful ON copilot_log (helpful)',
    'CREATE INDEX IF NOT EXISTS ix_copilot_log_pinned ON copilot_log (pinned)',
)

#: Added after the table shipped, so an existing install needs them too.
ADD_COLUMNS = (
    ('pinned', 'INTEGER DEFAULT 0'),
    ('pinned_at', 'DATETIME'),
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    print(f'database: {_db_url()}')
    engine = create_engine(_db_url())
    with engine.begin() as conn:
        print(f'  employees: {_guard(conn)}')
        exists = 'copilot_log' in inspect(engine).get_table_names()

        if args.down:
            if not exists:
                print('  nothing to drop.')
                return
            n = conn.execute(text('SELECT COUNT(*) FROM copilot_log')).scalar()
            print(f'  This DISCARDS {n} logged question(s).')
            if args.check:
                print('== DRY-RUN — nothing written ==')
                return
            if not args.yes:
                raise SystemExit('  Refusing without --yes.')
            conn.execute(text('DROP TABLE copilot_log'))
            print('  - copilot_log')
            return

        if args.check:
            print('== DRY-RUN — nothing written ==')
            print(f'  WOULD create: '
                  f'{"copilot_log" if not exists else "(already there)"}')
            print('  WOULD backfill: nothing. The log starts when the '
                  'Copilot does.')
            return

        conn.execute(text(DDL))
        have = {r[1] for r in conn.execute(text(
            'PRAGMA table_info(copilot_log)'))}
        for col, ddl in ADD_COLUMNS:
            if col not in have:
                conn.execute(text(
                    f'ALTER TABLE copilot_log ADD COLUMN {col} {ddl}'))
                print(f'  + copilot_log.{col}')
        for stmt in INDEXES:
            conn.execute(text(stmt))
        print('  + copilot_log' if not exists else '  copilot_log present')
        print('  indexes ensured')
    print('  done.')


if __name__ == '__main__':
    main()
