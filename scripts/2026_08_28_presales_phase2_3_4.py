"""
v2026-08 — Pre-Sales Intelligence · Phases 2, 3 & 4 migration.

Creates six new tables:
    projects
    project_updates
    project_stage_history
    project_accounts
    project_contacts
    opportunity_source_links

All additive. Idempotent — safe to run multiple times.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect
from app import app, db
from presales.models_projects import (
    Project, ProjectUpdate, ProjectStageHistory,
    ProjectAccount, ProjectContact, OpportunitySourceLink,
)

NEW_TABLES = [
    Project, ProjectUpdate, ProjectStageHistory,
    ProjectAccount, ProjectContact, OpportunitySourceLink,
]


def main(apply_changes: bool):
    with app.app_context():
        insp = inspect(db.engine)
        planned = []
        for model in NEW_TABLES:
            if insp.has_table(model.__tablename__):
                print(f'  = {model.__tablename__}: already exists')
            else:
                planned.append(model)
        if not planned:
            print('\nNothing to do.'); return
        if not apply_changes:
            print('\nPlanned:')
            for m in planned:
                print(f'  + CREATE TABLE {m.__tablename__}')
            print('\nDRY RUN — re-run with --apply.')
            return
        with db.engine.begin() as conn:
            for m in planned:
                m.__table__.create(bind=conn)
                print(f'  [OK] CREATE TABLE {m.__tablename__}')
        print('\nPhase 2/3/4 migration complete.')


if __name__ == '__main__':
    main(apply_changes='--apply' in sys.argv)
