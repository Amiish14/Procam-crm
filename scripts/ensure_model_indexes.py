"""
Create every index the models declare that an existing table lacks.

create_all at boot adds indexes only when it creates the table, so a
table that predates an index=True column (or was created by an older
migration's hand-written DDL) never gets it. The preflight reports these
as "indexes N missing"; this script adds them. Additive only: it creates
indexes and never drops or alters anything.

Unique indexes are listed but not created: on a table with duplicate
values the CREATE fails, and removing duplicates is a data decision.

Index DDL is generated from the models in a child process against an
in-memory database, as in the release migration; the production database
is opened with a plain engine.

Usage
    python scripts/ensure_model_indexes.py --check
    python scripts/ensure_model_indexes.py
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

_DDL = r'''
import io, contextlib, json, os
os.environ["DATABASE_URL"] = "sqlite://"
os.environ.setdefault("SECRET_KEY", "index-ddl-" + "x" * 32)
os.environ.setdefault("ADMIN_INITIAL_PASSWORD", "IndexDdlOnly-1234567")
import logging; logging.disable(logging.CRITICAL)
with contextlib.redirect_stdout(io.StringIO()):
    import app as A
    import app.models  # noqa
    for mod in ("app.models.copilot", "app.models.audit",
                "app.models.data_quality", "app.models.business_card",
                "app.models.tms_handover", "app.models.rfq",
                "app.models.quote"):
        try:
            __import__(mod)
        except Exception:
            pass
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateIndex
out = []
for t in A.db.metadata.sorted_tables:
    for i in t.indexes:
        if not i.name:
            continue
        out.append({"table": t.name, "name": i.name, "unique": bool(i.unique),
                    "columns": [c.name for c in i.columns],
                    "sql": str(CreateIndex(i).compile(dialect=sqlite.dialect()))
                           .strip()})
print("INDEXES" + json.dumps(out))
'''


def model_indexes():
    proc = subprocess.run([sys.executable, '-c', _DDL], cwd=_ROOT,
                          capture_output=True, text=True, timeout=180,
                          env=dict(os.environ, DATABASE_URL='sqlite://'))
    for line in proc.stdout.splitlines():
        if line.startswith('INDEXES'):
            return json.loads(line[len('INDEXES'):])
    raise SystemExit('could not read the indexes from the models:\n'
                     + proc.stderr[-800:])


def _db_url():
    return os.environ.get('DATABASE_URL') or (
        'sqlite:///' + os.path.join(_ROOT, 'procam_crm.db'))


def plan(conn, declared):
    """(to create, unique ones left alone) for tables that exist."""
    tables = {r[0] for r in conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table'"))}
    create, skipped = [], []
    for ix in declared:
        if ix['table'] not in tables:
            continue          # create_all builds the table with its indexes
        have = {r[1] for r in conn.execute(text(
            f'PRAGMA index_list("{ix["table"]}")'))}
        if ix['name'] in have:
            continue
        cols = {r[1] for r in conn.execute(text(
            f'PRAGMA table_info("{ix["table"]}")'))}
        if not set(ix['columns']) <= cols:
            continue          # the column autoheal adds it first
        (skipped if ix['unique'] else create).append(ix)
    return create, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true',
                    help='list what would be created; write nothing')
    args = ap.parse_args()

    print(f'database: {_db_url()}')
    declared = model_indexes()
    with create_engine(_db_url()).begin() as conn:
        try:
            n = conn.execute(text('SELECT COUNT(*) FROM employees')).scalar()
        except Exception:
            n = 0
        if not n:
            raise SystemExit('Refusing to run: no employees — not the '
                             'production database.')
        create, skipped = plan(conn, declared)
        for ix in skipped:
            print(f'  unique, not created: {ix["table"]}.{ix["name"]} '
                  '(check for duplicate values first)')
        if not create:
            print('  nothing to do — every declared index is present.')
            return
        for ix in create:
            print(f'  {"WOULD create" if args.check else "+"} '
                  f'{ix["table"]}.{ix["name"]} ({", ".join(ix["columns"])})')
        if args.check:
            print('== DRY RUN — nothing written ==')
            return
        for ix in create:
            conn.execute(text(ix['sql'].replace(
                'CREATE INDEX', 'CREATE INDEX IF NOT EXISTS', 1)))
    print(f'  done: {len(create)} index(es) created.')


if __name__ == '__main__':
    main()
