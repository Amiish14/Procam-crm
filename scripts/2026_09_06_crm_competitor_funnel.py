"""
CRM Competitor Master + Multiple Competitors + Public Source migration —
Phases 9 & 10 (Phase 11 is UI/routes-only, no tables to create).

Idempotent, additive.  Safe to rerun any time.

Steps:
    1. db.create_all() creates the new tables:
         competitor_masters
         opportunity_competitors
         competitor_contacts
         competitor_intelligence
         competitor_assessments
         public_sources
         public_source_items
    2. Backfills legacy per-lead `Competitor` (app.py:701) rows into the
       new master + junction tables:
         a. Upsert CompetitorMaster by lower(name).
         b. If the source Lead has an associated Opportunity (matched by
            Lead.opp_number), create an OpportunityCompetitor row
            with status='Confirmed'; else attach at lead_id only.
       Idempotency: skip a legacy row whose (competitor_id, lead_id)
       junction already exists.
    3. Seeds 1 new TaskDefinition:
         competitor.log  — fires when an Opportunity is marked Lost
                            without any confirmed competitor attached.
                            SLA 24 h.  Owner: primary opp owner.
    4. Prints a summary.

Usage:
    python scripts/2026_09_06_crm_competitor_funnel.py             # apply
    python scripts/2026_09_06_crm_competitor_funnel.py --check     # dry-run
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import app as flask_app, db   # noqa: E402


# ─── Seed data ─────────────────────────────────────────────────────────
# (task_key, title, module, allowed_roles, start_state, completion_state,
#  next_task_key, next_owner_rule, action_route, sla_hours,
#  escalation_role, priority)
SEED_TASK_DEFINITIONS = [
    ('competitor.log',
     'Log the competitor who won this deal',
     'competitor',
     ['Lead_Driver', 'Vertical_Head'],
     {'entity': 'Opportunity', 'state': 'Lost'},
     {'entity': 'OpportunityCompetitor', 'state': 'Confirmed'},
     None,
     {'kind': 'field', 'path': 'owner_emp_code'},
     '/opportunities/{entity_id}', 24, 'Vertical_Head', 2),
]


def _seed_defs(dry=False):
    from app.models.task_engine import TaskDefinition
    inserted = 0
    for (task_key, title, module, allowed_roles, start_state,
         completion_state, next_task_key, next_owner_rule, action_route,
         sla_hours, escalation_role, priority) in SEED_TASK_DEFINITIONS:
        if TaskDefinition.query.filter_by(task_key=task_key).first():
            continue
        if dry:
            inserted += 1
            continue
        db.session.add(TaskDefinition(
            task_key=task_key, title=title, module=module,
            allowed_roles=list(allowed_roles or []),
            start_state=start_state, completion_state=completion_state,
            next_task_key=next_task_key, next_owner_rule=next_owner_rule,
            action_route=action_route, sla_hours=sla_hours,
            escalation_role=escalation_role, priority=priority or 3,
            is_active=True,
        ))
        inserted += 1
    if not dry:
        db.session.commit()
    return inserted


def _backfill_competitors(dry=False):
    """Fold legacy Competitor(lead_id, name, ...) rows into
    CompetitorMaster + OpportunityCompetitor."""
    from sqlalchemy import func
    from app.models.competitor import CompetitorMaster, OpportunityCompetitor

    # Legacy per-lead Competitor still lives in app.py (top-level model)
    try:
        from app import Competitor as LegacyCompetitor, Lead, Opportunity
    except Exception as e:
        return {'error': f'legacy import failed: {e}',
                'masters_upserted': 0, 'junctions_created': 0}

    legacy_rows = LegacyCompetitor.query.all()
    masters_upserted = 0
    junctions_created = 0
    for row in legacy_rows:
        name = (row.name or '').strip()
        if not name:
            continue
        master = CompetitorMaster.query.filter(
            func.lower(CompetitorMaster.name) == name.lower()).first()
        if master is None:
            if dry:
                masters_upserted += 1
                continue
            master = CompetitorMaster(
                name=name,
                is_active=True,
                created_by_id=getattr(row, 'added_by', None) or None,
                strengths=(getattr(row, 'strength', None) or None),
                weaknesses=(getattr(row, 'weakness', None) or None),
                strategic_notes=(getattr(row, 'notes', None) or None),
            )
            db.session.add(master)
            db.session.flush()
            masters_upserted += 1

        # Find opportunity via Lead.opp_number
        opp_id = None
        try:
            lead = Lead.query.get(row.lead_id)
            if lead and lead.opp_number:
                opp = Opportunity.query.filter_by(
                    opp_number=lead.opp_number).first()
                if opp:
                    opp_id = opp.id
        except Exception:
            pass

        # Idempotency: skip if junction (competitor_id, lead_id) exists
        exists = OpportunityCompetitor.query.filter_by(
            competitor_id=master.id, lead_id=row.lead_id).first()
        if exists:
            continue
        if dry:
            junctions_created += 1
            continue
        db.session.add(OpportunityCompetitor(
            competitor_id=master.id,
            lead_id=row.lead_id,
            opportunity_id=opp_id,
            status='Confirmed',
            quoted_price=row.quoted_price,
            notes=row.notes,
            strengths_here=row.strength,
            weaknesses_here=row.weakness,
            added_by_id=(row.added_by or None),
        ))
        junctions_created += 1

    if not dry:
        db.session.commit()
    return {'masters_upserted': masters_upserted,
            'junctions_created': junctions_created,
            'legacy_total': len(legacy_rows)}


def _count_new_rows():
    counts = {}
    for tbl in ('competitor_masters', 'opportunity_competitors',
                'competitor_contacts', 'competitor_intelligence',
                'competitor_assessments', 'public_sources',
                'public_source_items'):
        try:
            n = db.session.execute(
                db.text(f'SELECT COUNT(*) FROM {tbl}')).scalar()
            counts[tbl] = int(n or 0)
        except Exception:
            counts[tbl] = None
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true',
                    help='dry-run — do not write')
    args = ap.parse_args()

    # Force new models to import so their tables register on metadata.
    import app.models.competitor      # noqa: F401
    import app.models.public_source   # noqa: F401
    import app.models.task_engine     # noqa: F401

    with flask_app.app_context():
        if args.check:
            print('== DRY-RUN — no writes will be performed ==')
            new_seeds = 0
            try:
                from app.models.task_engine import TaskDefinition
                for row in SEED_TASK_DEFINITIONS:
                    if not TaskDefinition.query.filter_by(task_key=row[0]).first():
                        new_seeds += 1
            except Exception as e:
                print(f'  [warn] task_definitions introspection failed: {e}')
                new_seeds = len(SEED_TASK_DEFINITIONS)

            print(f'  WOULD create tables via db.create_all() '
                  f'(existing tables untouched)')
            print(f'  WOULD seed NEW task_definitions:     {new_seeds}')

            try:
                bf = _backfill_competitors(dry=True)
                print(f'  legacy Competitor rows found:        {bf.get("legacy_total")}')
                print(f'  WOULD upsert CompetitorMaster:       {bf.get("masters_upserted")}')
                print(f'  WOULD create OpportunityCompetitor:  {bf.get("junctions_created")}')
            except Exception as e:
                print(f'  [warn] backfill dry-run failed: {e}')

            counts = _count_new_rows()
            print(f'  Current row counts (None = table missing):')
            for k, v in counts.items():
                print(f'    {k}: {v}')
            print('== end dry-run ==')
            return

        # Apply
        db.create_all()
        new_seeds = _seed_defs()
        bf = _backfill_competitors()
        counts = _count_new_rows()

        print('== 2026_09_06_crm_competitor_funnel.py — summary ==')
        print(f'  tables created / verified via db.create_all()')
        print(f'  new task_definitions seeded:            {new_seeds}')
        print(f'  legacy Competitor rows scanned:         {bf.get("legacy_total")}')
        print(f'  CompetitorMaster rows upserted:         {bf.get("masters_upserted")}')
        print(f'  OpportunityCompetitor rows created:     {bf.get("junctions_created")}')
        print(f'  current row counts:')
        for k, v in counts.items():
            print(f'    {k}: {v}')
        print('  done.')


if __name__ == '__main__':
    main()
