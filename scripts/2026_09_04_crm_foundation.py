"""
CRM foundation migration — Phases 2-5.

Idempotent, additive.  Safe to rerun any time.

Runs:
    1. db.create_all() for the new RBAC / task-engine / notification tables.
    2. Seeds 7 canonical crm_roles (Lead_Driver, Rate_Sourcing, ...).
    3. Seeds ~16 task_definitions covering the Lead → RFQ → Quote →
       Opportunity → Project lifecycle.
    4. Backfills member rows from the legacy single-owner columns:
         Lead.assigned_to        → LeadMember    (Lead_Driver, primary)
         Company.pic_emp_code    → AccountMember (Lead_Driver, primary)
         Opportunity.owner_emp_code → DealMember (Lead_Driver, primary)
         Project.pic_emp_code    → ProjectMember (Lead_Driver, primary)
    5. Alters employees table:
         ADD COLUMN default_landing_page VARCHAR(20) DEFAULT 'my_work'
    6. Prints a summary of rows seeded + backfilled.

Usage:
    python scripts/2026_09_04_crm_foundation.py            # apply
    python scripts/2026_09_04_crm_foundation.py --check    # dry-run only
"""
import os
import sys
import argparse
from datetime import datetime

# Ensure we can import the top-level app.py.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import app as flask_app, db, Employee, Lead, Company, Opportunity   # noqa: E402


# ─── Seed data ───────────────────────────────────────────────────────────────
SEED_TASK_DEFINITIONS = [
    # (task_key, title, module, allowed_roles, start_state, completion_state,
    #  next_task_key, next_owner_rule, action_route, sla_hours, escalation_role,
    #  priority)
    ('account.research',
     'Research a newly-created account',
     'accounts',
     ['Lead_Driver', 'Vertical_Head'],
     {'entity': 'Company', 'state': '__created__'},
     {'entity': 'Company', 'state': 'Target Identified'},
     'account.first_contact',
     {'kind': 'primary_owner'},
     '/app?account={entity_id}', 48, 'Vertical_Head', 3),

    ('account.first_contact',
     'Identify and reach out to a contact at this account',
     'accounts',
     ['Lead_Driver'],
     {'entity': 'Company', 'state': 'Target Identified'},
     {'entity': 'Company', 'state': 'Contact Made'},
     None,
     {'kind': 'primary_owner'},
     '/app?account={entity_id}', 72, 'Vertical_Head', 3),

    ('account.follow_up_due',
     'Follow up with this account',
     'accounts',
     ['Lead_Driver'],
     {'entity': 'Company', 'state': 'Follow-Up Due'},
     None,
     None,
     {'kind': 'primary_owner'},
     '/app?account={entity_id}', 24, 'Vertical_Head', 2),

    ('lead.first_contact',
     'First contact on a newly-created lead',
     'leads',
     ['Lead_Driver'],
     {'entity': 'Lead', 'state': '__created__'},
     {'entity': 'Lead', 'state': 'Call Done'},
     'lead.qualify',
     {'kind': 'field', 'path': 'assigned_to'},
     '/leads/{entity_id}', 24, 'Vertical_Head', 2),

    ('lead.qualify',
     'Qualify this lead',
     'leads',
     ['Lead_Driver'],
     {'entity': 'Lead', 'state': 'New'},
     {'entity': 'Lead', 'state': 'Profile Sent'},
     None,
     {'kind': 'primary_owner'},
     '/leads/{entity_id}', 48, 'Vertical_Head', 3),

    ('lead.rfq_review',
     'Review the RFQ generated on this lead',
     'leads',
     ['Lead_Driver', 'Rate_Sourcing'],
     {'entity': 'Lead', 'state': 'RFQ Generated'},
     {'entity': 'Lead', 'state': 'Quoted'},
     'rfq.rate_source',
     {'kind': 'primary_owner'},
     '/leads/{entity_id}', 24, 'Vertical_Head', 2),

    ('rfq.rate_source',
     'Source rates for this RFQ',
     'rfq',
     ['Rate_Sourcing'],
     {'entity': 'Lead', 'state': 'RFQ Dated'},
     {'entity': 'Lead', 'state': 'Rates Sourced'},
     'quote.prepare',
     {'kind': 'role', 'role': 'Rate_Sourcing'},
     '/leads/{entity_id}', 48, 'Vertical_Head', 2),

    ('quote.prepare',
     'Prepare the quote',
     'quotes',
     ['Lead_Driver', 'Commercial_Support'],
     {'entity': 'Lead', 'state': 'Rates Sourced'},
     {'entity': 'Lead', 'state': 'Quote Drafted'},
     'quote.approve',
     {'kind': 'primary_owner'},
     '/leads/{entity_id}', 24, 'Vertical_Head', 3),

    ('quote.approve',
     'Approve this quote before submission',
     'quotes',
     ['Vertical_Head'],
     {'entity': 'Quote', 'state': 'Submitted For Approval'},
     {'entity': 'Quote', 'state': 'Approved'},
     'quote.submit',
     {'kind': 'role', 'role': 'Vertical_Head'},
     '/app?quote={entity_id}', 8, 'Management_Sponsor', 1),

    ('quote.submit',
     'Submit the approved quote to the customer',
     'quotes',
     ['Lead_Driver'],
     {'entity': 'Quote', 'state': 'Approved'},
     {'entity': 'Quote', 'state': 'Submitted'},
     'quote.followup',
     {'kind': 'role', 'role': 'Lead_Driver'},
     '/app?quote={entity_id}', 24, 'Vertical_Head', 2),

    ('quote.followup',
     'Follow up on the submitted quote',
     'quotes',
     ['Lead_Driver'],
     {'entity': 'Lead', 'state': 'Quoted'},
     {'entity': 'Lead', 'state': 'Under Negotiation'},
     'deal.negotiation',
     {'kind': 'role', 'role': 'Lead_Driver'},
     '/leads/{entity_id}', 72, 'Vertical_Head', 3),

    ('deal.negotiation',
     'Negotiate the deal to closure',
     'opportunities',
     ['Lead_Driver'],
     {'entity': 'Opportunity', 'state': 'Under Negotiation'},
     {'entity': 'Opportunity', 'state': 'Won'},
     'deal.won.handoff',
     {'kind': 'role', 'role': 'Lead_Driver'},
     '/app?opp={entity_id}', 48, 'Vertical_Head', 2),

    ('deal.won.handoff',
     'Hand off the won deal to operations',
     'opportunities',
     ['Lead_Driver', 'Vertical_Head'],
     {'entity': 'Opportunity', 'state': 'Won'},
     {'entity': 'Opportunity', 'state': 'Handed Off'},
     None,
     {'kind': 'primary_owner'},
     '/app?opp={entity_id}', 24, 'Vertical_Head', 2),

    ('deal.lost.debrief',
     'Capture a lost-deal debrief',
     'opportunities',
     ['Lead_Driver', 'Vertical_Head'],
     {'entity': 'Opportunity', 'state': 'Lost'},
     None,
     'competitor.log',
     {'kind': 'primary_owner'},
     '/app?opp={entity_id}', 48, 'Vertical_Head', 3),

    ('project.review',
     'Review this project — no update in 14 days',
     'projects',
     ['Lead_Driver'],
     {'entity': 'Project', 'state': 'Review Due'},
     None,
     None,
     {'kind': 'primary_owner'},
     '/app?project={entity_id}', 48, 'Vertical_Head', 3),

    ('competitor.log',
     'Log which competitor won and why',
     'opportunities',
     ['Lead_Driver'],
     {'entity': 'Opportunity', 'state': 'Lost'},
     {'entity': 'Opportunity', 'state': 'Competitor Logged'},
     None,
     {'kind': 'primary_owner'},
     '/app?opp={entity_id}', 24, 'Vertical_Head', 3),
]


# ─── Column helpers ──────────────────────────────────────────────────────────
def _column_exists(table, column):
    try:
        from sqlalchemy import inspect as _inspect
        cols = [c['name'] for c in _inspect(db.engine).get_columns(table)]
        return column in cols
    except Exception:
        return False


def _add_column_if_missing(table, column, ddl):
    """ALTER TABLE … ADD COLUMN … IF NOT EXISTS (portable)."""
    if _column_exists(table, column):
        return False
    try:
        db.session.execute(db.text(f'ALTER TABLE {table} ADD COLUMN {ddl}'))
        db.session.commit()
        return True
    except Exception as e:
        db.session.rollback()
        print(f'  [warn] add {table}.{column} failed: {e}')
        return False


# ─── Actions ────────────────────────────────────────────────────────────────
def _seed_roles(dry=False):
    from app.models.rbac import Role, SEED_ROLES
    inserted = 0
    for key, name, desc in SEED_ROLES:
        if Role.query.filter_by(key=key).first():
            continue
        if dry:
            inserted += 1
            continue
        db.session.add(Role(key=key, name=name, description=desc,
                            is_system=True, is_active=True))
        inserted += 1
    if not dry:
        db.session.commit()
    return inserted


def _seed_task_definitions(dry=False):
    from app.models.task_engine import TaskDefinition
    inserted = 0
    for (task_key, title, module, allowed_roles, start_state, completion_state,
         next_task_key, next_owner_rule, action_route, sla_hours,
         escalation_role, priority) in SEED_TASK_DEFINITIONS:
        if TaskDefinition.query.filter_by(task_key=task_key).first():
            continue
        if dry:
            inserted += 1
            continue
        db.session.add(TaskDefinition(
            task_key=task_key,
            title=title,
            module=module,
            allowed_roles=list(allowed_roles or []),
            start_state=start_state,
            completion_state=completion_state,
            next_task_key=next_task_key,
            next_owner_rule=next_owner_rule,
            action_route=action_route,
            sla_hours=sla_hours,
            escalation_role=escalation_role,
            priority=priority or 3,
            is_active=True,
        ))
        inserted += 1
    if not dry:
        db.session.commit()
    return inserted


def _backfill_members(dry=False):
    """Backfill member rows from legacy single-owner columns."""
    from app.models.rbac import (Role, LeadMember, AccountMember, DealMember,
                                 ProjectMember)
    stats = {'lead': 0, 'account': 0, 'deal': 0, 'project': 0}

    role = Role.query.filter_by(key='Lead_Driver').first()
    if not role:
        # Roles must be seeded first
        return stats
    rid = role.id

    def _upsert(model, fk_col, fk_val, user_id):
        q = model.query.filter(getattr(model, fk_col) == fk_val,
                               model.user_id == user_id,
                               model.role_id == rid,
                               model.is_active.is_(True))
        if q.first():
            return False
        row = model(**{fk_col: fk_val})
        row.user_id = user_id
        row.role_id = rid
        row.is_primary = True
        row.assigned_by = 'migration:2026_09_04'
        row.assigned_at = datetime.utcnow()
        row.is_active = True
        row.remarks = 'Backfilled from legacy single-owner column.'
        db.session.add(row)
        return True

    # Lead.assigned_to → LeadMember
    for lid, assigned in db.session.query(Lead.id, Lead.assigned_to)\
                                    .filter(Lead.assigned_to.isnot(None),
                                            Lead.assigned_to != '').all():
        if not assigned:
            continue
        # skip if the employee doesn't exist (broken FK)
        if not Employee.query.filter_by(emp_code=assigned).first():
            continue
        if dry:
            stats['lead'] += 1
            continue
        if _upsert(LeadMember, 'lead_id', lid, assigned):
            stats['lead'] += 1

    # Company.pic_emp_code → AccountMember
    for aid, pic in db.session.query(Company.id, Company.pic_emp_code)\
                              .filter(Company.pic_emp_code.isnot(None),
                                      Company.pic_emp_code != '').all():
        if not pic:
            continue
        if not Employee.query.filter_by(emp_code=pic).first():
            continue
        if dry:
            stats['account'] += 1
            continue
        if _upsert(AccountMember, 'account_id', aid, pic):
            stats['account'] += 1

    # Opportunity.owner_emp_code → DealMember
    for oid, owner in db.session.query(Opportunity.id, Opportunity.owner_emp_code)\
                                 .filter(Opportunity.owner_emp_code.isnot(None),
                                         Opportunity.owner_emp_code != '').all():
        if not owner:
            continue
        if not Employee.query.filter_by(emp_code=owner).first():
            continue
        if dry:
            stats['deal'] += 1
            continue
        if _upsert(DealMember, 'opportunity_id', oid, owner):
            stats['deal'] += 1

    # Project.pic_emp_code → ProjectMember (presales.models_projects.Project)
    try:
        from presales.models_projects import Project as _Project
        for pid, pic in db.session.query(_Project.id, _Project.pic_emp_code)\
                                   .filter(_Project.pic_emp_code.isnot(None),
                                           _Project.pic_emp_code != '').all():
            if not pic:
                continue
            if not Employee.query.filter_by(emp_code=pic).first():
                continue
            if dry:
                stats['project'] += 1
                continue
            if _upsert(ProjectMember, 'project_id', pid, pic):
                stats['project'] += 1
    except Exception as e:
        print(f'  [info] project backfill skipped: {e}')

    if not dry:
        db.session.commit()
    return stats


# ─── Entrypoint ─────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true',
                    help='dry-run: print what would change, do not touch DB')
    args = ap.parse_args()

    # Ensure the new model modules are imported so their tables register.
    # (These live under the app/ package via app.py's __path__ trick.)
    import app.models.rbac          # noqa: F401
    import app.models.task_engine   # noqa: F401
    import app.models.notification  # noqa: F401

    with flask_app.app_context():

        if args.check:
            print('== DRY-RUN — no writes will be performed ==')
            # We still need to know how many rows WOULD be seeded / backfilled.
            # Roles + task defs: report how many are missing.
            try:
                from app.models.rbac import Role, SEED_ROLES
                from app.models.task_engine import TaskDefinition
                missing_roles = sum(
                    1 for key, _n, _d in SEED_ROLES
                    if not Role.query.filter_by(key=key).first())
            except Exception:
                missing_roles = len(_import_seed_role_keys())
            try:
                missing_tasks = sum(
                    1 for row in SEED_TASK_DEFINITIONS
                    if not TaskDefinition.query.filter_by(task_key=row[0]).first())
            except Exception:
                missing_tasks = len(SEED_TASK_DEFINITIONS)
            # Backfill: count rows with a legacy owner set.
            try:
                n_lead = Lead.query.filter(Lead.assigned_to.isnot(None),
                                           Lead.assigned_to != '').count()
            except Exception:
                n_lead = 0
            try:
                n_acct = Company.query.filter(
                    Company.pic_emp_code.isnot(None),
                    Company.pic_emp_code != '').count()
            except Exception:
                n_acct = 0
            try:
                n_deal = Opportunity.query.filter(
                    Opportunity.owner_emp_code.isnot(None),
                    Opportunity.owner_emp_code != '').count()
            except Exception:
                n_deal = 0
            try:
                from presales.models_projects import Project as _P
                n_prj = _P.query.filter(_P.pic_emp_code.isnot(None),
                                        _P.pic_emp_code != '').count()
            except Exception:
                n_prj = 0
            print(f'  WOULD seed roles:            {missing_roles}')
            print(f'  WOULD seed task_definitions: {missing_tasks}')
            print(f'  WOULD backfill LeadMember:    up to {n_lead}')
            print(f'  WOULD backfill AccountMember: up to {n_acct}')
            print(f'  WOULD backfill DealMember:    up to {n_deal}')
            print(f'  WOULD backfill ProjectMember: up to {n_prj}')
            print(f'  WOULD add employees.default_landing_page '
                  f'(present={_column_exists("employees", "default_landing_page")})')
            print('== end dry-run ==')
            return

        # 1) create all new tables (existing tables are untouched).
        db.create_all()

        # 5) additive column on employees
        added = _add_column_if_missing(
            'employees', 'default_landing_page',
            "default_landing_page VARCHAR(20) DEFAULT 'my_work'")

        # 2) seed roles
        n_roles = _seed_roles()

        # 3) seed task definitions
        n_tasks = _seed_task_definitions()

        # 4) backfill members
        stats = _backfill_members()

        print('== 2026_09_04_crm_foundation.py — summary ==')
        print(f'  roles seeded:                {n_roles}')
        print(f'  task_definitions seeded:     {n_tasks}')
        print(f'  LeadMember backfilled:       {stats["lead"]}')
        print(f'  AccountMember backfilled:    {stats["account"]}')
        print(f'  DealMember backfilled:       {stats["deal"]}')
        print(f'  ProjectMember backfilled:    {stats["project"]}')
        print(f'  employees.default_landing_page added: {added}')
        print('  done.')


def _import_seed_role_keys():
    try:
        from app.models.rbac import SEED_ROLES
        return [r[0] for r in SEED_ROLES]
    except Exception:
        return []


if __name__ == '__main__':
    main()
