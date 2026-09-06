"""
CRM RFQ + Quote + TMS Handover migration — Phases 6-8.

Idempotent, additive.  Safe to rerun any time.

Steps:
    1. db.create_all() creates the new tables (rfqs, rfq_attachments,
       rate_sourcing_lines, quotes, quote_lines, quote_revision_logs,
       won_handovers).
    2. Seeds 4 new TaskDefinitions:
         rfq.review          — on RFQ 'Received'
         rfq.rate_source     — on RateSourcingLine 'Not Started'
         rfq.rate_partial    — on RateSourcingLine 'Partially Received'
         rfq.followup_stale  — on RFQ 'Quoted' (no update 3d)
       Also refreshes / upserts these quote-related defs to point at the
       Quote entity (Foundation drop's start_states were generic):
         quote.prepare / quote.approve / quote.submit / quote.followup
       And adds:
         deal.won.handoff (Opportunity Won) — refreshed if present
    3. Prints a summary.

Usage:
    python scripts/2026_09_05_crm_rfq_quote_tms.py             # apply
    python scripts/2026_09_05_crm_rfq_quote_tms.py --check     # dry-run
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import app as flask_app, db   # noqa: E402


# ─── Seed data ───────────────────────────────────────────────────────────────
# (task_key, title, module, allowed_roles, start_state, completion_state,
#  next_task_key, next_owner_rule, action_route, sla_hours,
#  escalation_role, priority)
SEED_TASK_DEFINITIONS = [
    ('rfq.review',
     'Review a newly-received RFQ',
     'rfq',
     ['Lead_Driver', 'Vertical_Head'],
     {'entity': 'RFQ', 'state': 'Received'},
     {'entity': 'RFQ', 'state': 'Rate Sourcing'},
     'rfq.rate_source',
     {'kind': 'field', 'path': 'lead_driver'},
     '/rfqs/{entity_id}', 24, 'Vertical_Head', 2),

    ('rfq.rate_partial',
     'Rate line partially received — escalate',
     'rfq',
     ['Rate_Sourcing', 'Vertical_Head'],
     {'entity': 'RateSourcingLine', 'state': 'Partially Received'},
     {'entity': 'RateSourcingLine', 'state': 'Completed'},
     None,
     {'kind': 'role', 'role': 'Rate_Sourcing'},
     '/rfqs/{entity_id}', 24, 'Vertical_Head', 2),

    ('rfq.followup_stale',
     'RFQ has not moved in 3 days — follow up',
     'rfq',
     ['Lead_Driver'],
     {'entity': 'RFQ', 'state': 'Quoted'},
     {'entity': 'RFQ', 'state': 'Negotiation'},
     None,
     {'kind': 'field', 'path': 'lead_driver'},
     '/rfqs/{entity_id}', 72, 'Vertical_Head', 3),
]

# Task defs the Foundation drop already seeded, but which we need to
# retune (start_state / completion_state / action_route) now that the
# Quote entity actually exists.  Upsert = update-in-place if present.
UPSERT_TASK_DEFINITIONS = [
    ('rfq.rate_source',
     'Source rates for this line',
     'rfq',
     ['Rate_Sourcing'],
     {'entity': 'RateSourcingLine', 'state': 'Not Started'},
     {'entity': 'RateSourcingLine', 'state': 'Completed'},
     None,
     {'kind': 'field', 'path': 'sourcing_owner_id'},
     '/rfqs/{entity_id}', 48, 'Vertical_Head', 2),

    ('quote.prepare',
     'Prepare the quote',
     'quotes',
     ['Lead_Driver', 'Commercial_Support'],
     {'entity': 'Quote', 'state': 'Draft'},
     {'entity': 'Quote', 'state': 'Awaiting Approval'},
     'quote.approve',
     {'kind': 'field', 'path': 'prepared_by_id'},
     '/quotes/{entity_id}', 24, 'Vertical_Head', 3),

    ('quote.approve',
     'Approve this quote before submission',
     'quotes',
     ['Vertical_Head'],
     {'entity': 'Quote', 'state': 'Awaiting Approval'},
     {'entity': 'Quote', 'state': 'Approved'},
     'quote.submit',
     {'kind': 'role', 'role': 'Vertical_Head'},
     '/quotes/{entity_id}', 8, 'Management_Sponsor', 1),

    ('quote.submit',
     'Submit the approved quote to the customer',
     'quotes',
     ['Lead_Driver'],
     {'entity': 'Quote', 'state': 'Approved'},
     {'entity': 'Quote', 'state': 'Submitted'},
     'quote.followup',
     {'kind': 'field', 'path': 'prepared_by_id'},
     '/quotes/{entity_id}', 24, 'Vertical_Head', 2),

    ('quote.followup',
     'Follow up on the submitted quote',
     'quotes',
     ['Lead_Driver'],
     {'entity': 'Quote', 'state': 'Submitted'},
     {'entity': 'Quote', 'state': 'Won'},
     None,
     {'kind': 'field', 'path': 'prepared_by_id'},
     '/quotes/{entity_id}', 72, 'Vertical_Head', 3),

    ('deal.won.handoff',
     'Hand off the won deal to TMS Operations',
     'handover',
     ['Lead_Driver', 'Vertical_Head'],
     {'entity': 'WonHandover', 'state': 'Handover Pending'},
     {'entity': 'WonHandover', 'state': 'TMS Project Created'},
     None,
     {'kind': 'field', 'path': 'pic_emp_code'},
     '/handovers', 24, 'Vertical_Head', 2),
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


def _upsert_defs(dry=False):
    from app.models.task_engine import TaskDefinition
    updated = 0
    inserted = 0
    for (task_key, title, module, allowed_roles, start_state,
         completion_state, next_task_key, next_owner_rule, action_route,
         sla_hours, escalation_role, priority) in UPSERT_TASK_DEFINITIONS:
        row = TaskDefinition.query.filter_by(task_key=task_key).first()
        if row is None:
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
        else:
            if dry:
                updated += 1
                continue
            row.title = title
            row.module = module
            row.allowed_roles = list(allowed_roles or [])
            row.start_state = start_state
            row.completion_state = completion_state
            row.next_task_key = next_task_key
            row.next_owner_rule = next_owner_rule
            row.action_route = action_route
            row.sla_hours = sla_hours
            row.escalation_role = escalation_role
            row.priority = priority or 3
            row.is_active = True
            updated += 1
    if not dry:
        db.session.commit()
    return inserted, updated


def _count_new_rows():
    """Report row counts in each new table (best-effort)."""
    counts = {}
    for tbl in ('rfqs', 'rfq_attachments', 'rate_sourcing_lines',
                'quotes', 'quote_lines', 'quote_revision_logs',
                'won_handovers'):
        try:
            n = db.session.execute(
                db.text(f'SELECT COUNT(*) FROM {tbl}')).scalar()
            counts[tbl] = int(n or 0)
        except Exception:
            counts[tbl] = None   # table missing (dry-run before create_all)
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true',
                    help='dry-run — do not write')
    args = ap.parse_args()

    # Force the new models to import so their tables register on metadata.
    import app.models.rfq            # noqa: F401
    import app.models.quote          # noqa: F401
    import app.models.tms_handover   # noqa: F401
    import app.models.task_engine    # noqa: F401
    import app.models.rbac           # noqa: F401
    import app.models.notification   # noqa: F401

    with flask_app.app_context():
        if args.check:
            print('== DRY-RUN — no writes will be performed ==')
            try:
                new_seeds = 0
                from app.models.task_engine import TaskDefinition
                for row in SEED_TASK_DEFINITIONS:
                    if not TaskDefinition.query.filter_by(task_key=row[0]).first():
                        new_seeds += 1
                new_upserts = 0
                to_update = 0
                for row in UPSERT_TASK_DEFINITIONS:
                    existing = TaskDefinition.query.filter_by(
                        task_key=row[0]).first()
                    if existing is None:
                        new_upserts += 1
                    else:
                        to_update += 1
            except Exception as e:
                print(f'  [warn] introspection failed: {e}')
                new_seeds = len(SEED_TASK_DEFINITIONS)
                new_upserts = 0
                to_update = 0
            print(f'  WOULD create tables via db.create_all() '
                  f'(existing tables untouched)')
            print(f'  WOULD seed NEW task_definitions:     {new_seeds}')
            print(f'  WOULD insert missing upsert-defs:    {new_upserts}')
            print(f'  WOULD refresh existing upsert-defs:  {to_update}')
            counts = _count_new_rows()
            print(f'  Current row counts (None = table missing):')
            for k, v in counts.items():
                print(f'    {k}: {v}')
            print('== end dry-run ==')
            return

        # Apply
        db.create_all()
        new_seeds = _seed_defs()
        new_upserts, updated = _upsert_defs()
        counts = _count_new_rows()

        print('== 2026_09_05_crm_rfq_quote_tms.py — summary ==')
        print(f'  tables created / verified via db.create_all()')
        print(f'  new task_definitions seeded:            {new_seeds}')
        print(f'  new upsert task_definitions inserted:   {new_upserts}')
        print(f'  existing task_definitions refreshed:    {updated}')
        print(f'  current row counts:')
        for k, v in counts.items():
            print(f'    {k}: {v}')
        print('  done.')


if __name__ == '__main__':
    main()
