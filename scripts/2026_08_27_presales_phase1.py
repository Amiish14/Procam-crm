"""
v2026-08 — Pre-Sales Intelligence · Phase 1 migration.

Additive-only. Idempotent. Never drops or renames.

Adds columns to existing tables:
    companies:   dev_stage, pic_emp_code, strategic_flag, priority,
                 last_activity_at, next_action_at, parent_account_id
    contacts:    account_id, is_active, relationship_strength, decision_role
    opportunities: source_type, source_account_id, source_project_id

Creates new tables:
    account_relationship_tags
    account_assignments
    account_stage_history
    account_activities

Usage:
    python scripts/2026_08_27_presales_phase1.py           # dry-run
    python scripts/2026_08_27_presales_phase1.py --apply
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect, text
from app import app, db
from presales.models import (
    AccountRelationshipTag, AccountAssignmentHistory,
    AccountStageHistory, AccountActivity,
)

NEW_COLUMNS = {
    'companies': [
        ('dev_stage',          'VARCHAR(60)'),
        ('pic_emp_code',       'VARCHAR(20)'),
        ('strategic_flag',     'BOOLEAN DEFAULT FALSE'),
        ('priority',           "VARCHAR(20) DEFAULT 'Medium'"),
        ('last_activity_at',   'TIMESTAMP'),
        ('next_action_at',     'DATE'),
        ('parent_account_id',  'INTEGER'),
    ],
    'contacts': [
        ('account_id',            'INTEGER'),
        ('is_active',             'BOOLEAN DEFAULT TRUE'),
        ('relationship_strength', 'VARCHAR(20)'),
        ('decision_role',         'VARCHAR(40)'),
    ],
    'opportunities': [
        ('source_type',         'VARCHAR(40)'),
        ('source_account_id',   'INTEGER'),
        ('source_project_id',   'INTEGER'),
    ],
}

NEW_TABLES = [
    AccountRelationshipTag,
    AccountAssignmentHistory,
    AccountStageHistory,
    AccountActivity,
]


def main(apply_changes: bool):
    with app.app_context():
        insp = inspect(db.engine)
        dialect = db.engine.dialect.name
        planned = []

        # Add columns
        for table, cols in NEW_COLUMNS.items():
            if not insp.has_table(table):
                print(f'  = {table}: table not present — skipping')
                continue
            existing = {c['name'] for c in insp.get_columns(table)}
            for name, col_type in cols:
                if name in existing:
                    print(f'  = {table}.{name}: already present')
                else:
                    planned.append(('add_col', table, name, col_type))

        # Create tables
        for model in NEW_TABLES:
            if insp.has_table(model.__tablename__):
                print(f'  = {model.__tablename__}: already exists')
            else:
                planned.append(('create', model, None, None))

        if not planned:
            print('\nNothing to do.')
            return

        if not apply_changes:
            print('\nPlanned actions:')
            for kind, a, b, c in planned:
                if kind == 'add_col':
                    print(f'  + ALTER TABLE {a} ADD COLUMN {b} {c}')
                elif kind == 'create':
                    print(f'  + CREATE TABLE {a.__tablename__}')
            print('\nDRY RUN — re-run with --apply.')
            return

        # Execute
        with db.engine.begin() as conn:
            for kind, a, b, c in planned:
                if kind == 'add_col':
                    ddl = f'ALTER TABLE {a} ADD COLUMN {b} {c}'
                    # SQLite dialect-friendly BOOLEAN handling
                    if dialect == 'sqlite' and 'BOOLEAN' in c.upper():
                        ddl = ddl.replace('BOOLEAN DEFAULT FALSE',
                                          "INTEGER DEFAULT 0")
                        ddl = ddl.replace('BOOLEAN DEFAULT TRUE',
                                          "INTEGER DEFAULT 1")
                    conn.execute(text(ddl))
                    print(f'  [OK] {ddl}')
                elif kind == 'create':
                    a.__table__.create(bind=conn)
                    print(f'  [OK] CREATE TABLE {a.__tablename__}')

        print('\nPhase 1 migration complete.')


if __name__ == '__main__':
    main(apply_changes='--apply' in sys.argv)
