"""
Schema for the production-hardening release. Additive only.

Adds
    audit_events                         the general audit trail (table)
    data_quality_snapshots               daily data-quality counts (table)
    employees.session_version            ends other sessions on reset
    employees.failed_logins              sign-in lockout counter
    employees.locked_until               sign-in lock expiry
    employees.temp_password_expires_at   temporary password expiry
    vendor_domains.name/notes/updated_at/updated_by   Vendor Master
    indexes on leads (owner, stage, dates), contacts (owner),
    opportunities (stage)
    plus the tables and columns listed in RELEASE_TABLES / RELEASE_COLUMNS

The app creates all of this at boot as well (create_all and the autoheal),
so a restart before this script does not break anything; the script is
the documented, reviewable --check and --down.

Table DDL is not written by hand: a child process imports the app against
an in-memory database and emits CREATE statements from the models, so what
this script creates is exactly what the code expects. The production
database itself is opened with a plain engine, never by importing the app.

Usage
    python scripts/2026_10_04_production_hardening.py --check
    python scripts/2026_10_04_production_hardening.py
    python scripts/2026_10_04_production_hardening.py --down --yes

Back up first:
    .venv/bin/python scripts/backup_database.py --label pre-migration
"""
import argparse
import json
import os
import subprocess
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

#: New tables in this release (their model modules are imported below).
RELEASE_TABLES = ['audit_events', 'data_quality_snapshots']
#: Modules whose import registers those models.
MODEL_MODULES = ['app.models.audit', 'app.models.data_quality']
#: New nullable columns on existing tables: (table, column, SQLite type).
RELEASE_COLUMNS = [
    ('employees', 'session_version', 'INTEGER DEFAULT 0'),
    ('employees', 'failed_logins', 'INTEGER DEFAULT 0'),
    ('employees', 'locked_until', 'DATETIME'),
    ('employees', 'temp_password_expires_at', 'DATETIME'),
    ('vendor_domains', 'name', 'VARCHAR(200)'),
    ('vendor_domains', 'notes', 'TEXT'),
    ('vendor_domains', 'updated_at', 'DATETIME'),
    ('vendor_domains', 'updated_by', 'VARCHAR(20)'),
]

_DDL = r'''
import io, contextlib, json, os, sys
os.environ["DATABASE_URL"] = "sqlite://"
os.environ.setdefault("SECRET_KEY", "migration-ddl-" + "x" * 32)
os.environ.setdefault("ADMIN_INITIAL_PASSWORD", "MigrationDdlOnly-123456")
import logging; logging.disable(logging.CRITICAL)
with contextlib.redirect_stdout(io.StringIO()):
    import app as A
    for mod in sys.argv[2].split(","):
        __import__(mod)
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateIndex, CreateTable
out = {"tables": {}, "indexes": [list(x) for x in A.BOOT_INDEXES]}
for name in sys.argv[1].split(","):
    t = A.db.metadata.tables[name]
    out["tables"][name] = {
        "create": str(CreateTable(t).compile(dialect=sqlite.dialect())).strip(),
        "indexes": [str(CreateIndex(i).compile(dialect=sqlite.dialect()))
                    .strip().replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS", 1)
                    for i in t.indexes],
    }
print("DDL" + json.dumps(out))
'''


def model_ddl():
    proc = subprocess.run(
        [sys.executable, '-c', _DDL, ','.join(RELEASE_TABLES),
         ','.join(MODEL_MODULES)],
        cwd=_ROOT, capture_output=True, text=True, timeout=180,
        env=dict(os.environ, DATABASE_URL='sqlite://'))
    for line in proc.stdout.splitlines():
        if line.startswith('DDL'):
            return json.loads(line[3:])
    raise SystemExit('could not build DDL from the models:\n'
                     + proc.stderr[-800:])


def _db_url():
    return os.environ.get('DATABASE_URL') or (
        'sqlite:///' + os.path.join(_ROOT, 'procam_crm.db'))


def _guard(conn):
    try:
        n = conn.execute(text('SELECT COUNT(*) FROM employees')).scalar()
    except Exception:
        n = 0
    if not n:
        raise SystemExit('Refusing to run: no employees — not the '
                         'production database.')
    return n


def plan(conn, ddl):
    tables = {r[0] for r in conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table'"))}
    indexes = {r[0] for r in conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='index'"))}
    steps = []
    for name, spec in ddl['tables'].items():
        if name not in tables:
            steps.append(('table', name, spec['create']))
        for stmt in spec['indexes']:
            ix = stmt.split(' IF NOT EXISTS ', 1)[1].split(' ', 1)[0]
            if ix not in indexes:
                steps.append(('index', ix, stmt))
    for table, col, typ in RELEASE_COLUMNS:
        have = {r[1] for r in conn.execute(text(
            f'PRAGMA table_info("{table}")'))}
        if col not in have:
            steps.append(('column', f'{table}.{col}',
                          f'ALTER TABLE {table} ADD COLUMN {col} {typ}'))
    for ix, table, col in ddl['indexes']:
        if ix not in indexes:
            steps.append(('index', ix,
                          f'CREATE INDEX IF NOT EXISTS {ix} ON {table} ({col})'))
    return steps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    print(f'database: {_db_url()}')
    ddl = model_ddl()
    with create_engine(_db_url()).begin() as conn:
        print(f'  employees: {_guard(conn)}')

        if args.down:
            rows = {}
            for name in RELEASE_TABLES:
                try:
                    rows[name] = conn.execute(text(
                        f'SELECT COUNT(*) FROM "{name}"')).scalar()
                except Exception:
                    rows[name] = None
            print('  --down DROPS the release tables and their data:')
            for name, n in rows.items():
                print(f'    {name}: {n if n is not None else "absent"} rows'
                      + ('  ← the audit trail; this cannot be undone'
                         if name == 'audit_events' else ''))
            print('  and the release columns (session versions, lockout '
                  'state, temporary password expiry).')
            print('  Almost never needed: older code ignores all of these.')
            if args.check or not args.yes:
                print('== nothing dropped (pass --yes without --check) ==')
                return
            for name in RELEASE_TABLES:
                conn.execute(text(f'DROP TABLE IF EXISTS "{name}"'))
            for table, col, _typ in RELEASE_COLUMNS:
                have = {r[1] for r in conn.execute(text(
                    f'PRAGMA table_info("{table}")'))}
                if col in have:
                    conn.execute(text(f'ALTER TABLE {table} DROP COLUMN {col}'))
            print('  dropped. Restart the app only after rolling back code, '
                  'or the boot recreates them.')
            return

        steps = plan(conn, ddl)
        if not steps:
            print('  nothing to do — schema already current.')
            return
        for kind, name, _sql in steps:
            print(f'  {"WOULD add" if args.check else "+"} {kind:<6} {name}')
        if args.check:
            print('== DRY RUN — nothing written ==')
            return
        for _kind, _name, sql in steps:
            conn.execute(text(sql))
    print('  done.')


if __name__ == '__main__':
    main()
