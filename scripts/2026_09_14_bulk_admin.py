#!/usr/bin/env python3
"""
Soft delete and deletion audit — §57, §58.

    python scripts/2026_09_14_bulk_admin.py --check
    python scripts/2026_09_14_bulk_admin.py

Adds leads.is_archived / archived_at / archived_by / archive_reason, and
the deletion_audit table.  Existing leads are all active — archiving is
something a person chooses, never something a migration decides.

Idempotent.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)


def _resolve_db_url():
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_ROOT, '.env'))
    except Exception:
        pass
    url = os.environ.get('DATABASE_URL', 'sqlite:///procam_crm.db')
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql://', 1)
    if url.startswith('sqlite:///') and not url.startswith('sqlite:////'):
        rel = url[len('sqlite:///'):]
        if not os.path.isabs(rel):
            cand = os.path.join(_ROOT, 'instance', rel)
            if not os.path.exists(cand):
                alt = os.path.join(_ROOT, rel)
                cand = alt if os.path.exists(alt) else cand
            url = 'sqlite:///' + cand
    return url


COLUMNS = [
    ('is_archived',    'BOOLEAN DEFAULT 0'),
    ('archived_at',    'DATETIME'),
    ('archived_by',    'VARCHAR(20)'),
    ('archive_reason', 'VARCHAR(200)'),
]


def add_columns(dry):
    from sqlalchemy import create_engine, inspect, text

    url = _resolve_db_url()
    if url.startswith('sqlite:///'):
        print(f'database: {url[len("sqlite:///"):]}')
    engine = create_engine(url)

    with engine.connect() as conn:
        n = conn.execute(text('SELECT COUNT(*) FROM employees')).scalar()
    print(f'employees in this database: {n}')
    if not n:
        print('!! no employees here — this is not the live database.')
        engine.dispose()
        return False

    existing = {c['name'] for c in inspect(engine).get_columns('leads')}
    todo = [(name, ddl) for name, ddl in COLUMNS if name not in existing]
    if not todo:
        print('leads soft-delete columns — already present')
        engine.dispose()
        return True

    for name, ddl in todo:
        print(f'ADD COLUMN leads.{name}' + ('   (dry run)' if dry else ''))
        if not dry:
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE leads ADD COLUMN {name} {ddl}'))
    engine.dispose()
    return not dry


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args()
    dry = args.check

    ready = add_columns(dry)
    if not ready:
        print('\n(dry run: re-run without --check to add the columns)'
              if dry else '\nAborted.')
        return 0 if dry else 1

    import importlib
    _main = importlib.import_module('app')
    importlib.import_module('app.models')
    app, db = _main.app, _main.db
    from app.models.audit import DeletionAudit

    with app.app_context():
        insp = db.inspect(db.engine)
        if 'deletion_audit' not in insp.get_table_names():
            print('CREATE TABLE deletion_audit')
            DeletionAudit.__table__.create(db.engine)
        else:
            print('deletion_audit — already present')

        total = _main.Lead.query.count()
        archived = _main.Lead.query.filter(
            _main.Lead.is_archived.is_(True)).count()
        print(f'\nLeads: {total} total, {archived} archived, '
              f'{total - archived} active')
        print('\n✓ applied.  No lead was archived by this migration — '
              'archiving is a decision, not a default.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
