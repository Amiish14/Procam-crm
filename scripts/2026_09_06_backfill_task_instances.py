#!/usr/bin/env python
"""
v2026-09-06 — Seed My Work from the CRM's existing position.

The Phase-2 migration backfilled *assignments* (who owns what) but not
*tasks*: the engine creates those from workflow events, and no event has
fired since deploy. My Work is therefore empty for everyone, which reads
as broken rather than as "nothing pending" — poor for a page that is now
the default landing for 28 of 29 users.

This walks every lead that already has an owner and raises the ONE task
its current stage implies, so My Work reflects the real position of the
pipeline from the first login.

Stage → task, using only task_keys the migrations already seeded:

    New                  -> lead.first_contact
    Call Done            -> lead.qualify
    Profile Sent         -> lead.qualify
    Business Discussion  -> lead.qualify
    Appointment          -> lead.qualify
    Visit Done           -> lead.qualify
    RFQ Generated        -> lead.rfq_review
    Quoted               -> quote.followup
    Under Negotiation    -> deal.negotiation
    Won                  -> deal.won.handoff      (only if no handover row)
    Lost                 -> deal.lost.debrief     (only if no competitor logged)

Due dates are derived from the lead's own history, not from today, so a
lead untouched for three weeks appears overdue immediately — which is the
truth, and the point of the exercise. Order of preference:

    1. followup_date, when one is set
    2. updated_at + the definition's sla_hours
    3. now + sla_hours

Idempotent: a lead that already has an open task is skipped, so this can
be re-run after more leads are created.

Examples:
    python scripts/2026_09_06_backfill_task_instances.py            # preview
    python scripts/2026_09_06_backfill_task_instances.py --apply
    python scripts/2026_09_06_backfill_task_instances.py --apply --include-closed
    python scripts/2026_09_06_backfill_task_instances.py --undo     # remove them
"""
import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
except ImportError:                                              # pragma: no cover
    pass

from app import app, db, Lead, Employee                       # noqa: E402
from app.models.task_engine import (TaskDefinition, TaskInstance,   # noqa: E402
                                    TaskInstanceStatus)

# Marker written into notes so --undo can find exactly these rows and
# nothing a human or the engine created afterwards.
MARKER = '[seeded by 2026_09_06 backfill]'

STAGE_TASK = {
    'New':                 'lead.first_contact',
    'Call Done':           'lead.qualify',
    'Profile Sent':        'lead.qualify',
    'Business Discussion': 'lead.qualify',
    'Appointment':         'lead.qualify',
    'Visit Done':          'lead.qualify',
    'RFQ Generated':       'lead.rfq_review',
    'Quoted':              'quote.followup',
    'Under Negotiation':   'deal.negotiation',
}
CLOSED_TASK = {
    'Won':  'deal.won.handoff',
    'Lost': 'deal.lost.debrief',
}


def _has_handover(lead_id):
    try:
        from app.models.tms_handover import WonHandover
        return WonHandover.query.filter_by(opportunity_id=lead_id).first() is not None
    except Exception:                                            # noqa: BLE001
        return False


def _has_competitor(lead_id):
    try:
        from app.models.competitor import OpportunityCompetitor
        return OpportunityCompetitor.query.filter_by(
            opportunity_id=lead_id).first() is not None
    except Exception:                                            # noqa: BLE001
        return False


def _due_for(lead, sla_hours):
    """When this task should have been done, from the lead's own history."""
    if getattr(lead, 'followup_date', None):
        return datetime.combine(lead.followup_date, datetime.min.time())
    base = getattr(lead, 'updated_at', None) or getattr(lead, 'created_at', None)
    hours = sla_hours or 48
    if base:
        return base + timedelta(hours=hours)
    return datetime.utcnow() + timedelta(hours=hours)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true', help='write the tasks')
    ap.add_argument('--undo', action='store_true',
                    help='delete tasks this script created, and nothing else')
    ap.add_argument('--include-closed', action='store_true',
                    help='also raise handover / debrief tasks on Won and Lost')
    ap.add_argument('--limit', type=int)
    args = ap.parse_args()

    with app.app_context():
        # ── undo ──────────────────────────────────────────────────────
        if args.undo:
            q = TaskInstance.query.filter(TaskInstance.notes.like('%' + MARKER + '%'))
            n = q.count()
            print(f'tasks created by this script: {n}')
            if not args.apply:
                print('\nPreview only. Add --apply to delete them.')
                return
            q.delete(synchronize_session=False)
            db.session.commit()
            print(f'deleted {n} seeded tasks. Anything created by the engine '
                  f'or by a person is untouched.')
            return

        defs = {d.task_key: d for d in TaskDefinition.query.all()}
        if not defs:
            sys.exit('No task_definitions found — run the Phase-2 migration first.')

        # Owners must be real, active employees or the task has no home.
        valid_owner = {e.emp_code for e in
                       Employee.query.filter_by(is_active=True).all()}

        # Leads that already have an open task are left alone.
        already = {(t.entity_type, t.entity_id) for t in
                   TaskInstance.query.filter(
                       TaskInstance.status.in_(TaskInstanceStatus.OPEN)).all()}

        wanted = dict(STAGE_TASK)
        if args.include_closed:
            wanted.update(CLOSED_TASK)

        leads = (Lead.query
                 .filter(Lead.assigned_to.isnot(None), Lead.assigned_to != '')
                 .order_by(Lead.id).all())
        if args.limit:
            leads = leads[:args.limit]

        print(f'leads with an owner : {len(leads)}')
        print(f'task definitions    : {len(defs)}')
        print(f'already have a task : {len(already)}')
        if not args.apply:
            print('\nPREVIEW — nothing will be written.\n')

        out = Counter()
        now = datetime.utcnow()
        created = []

        for lead in leads:
            owner = (lead.assigned_to or '').strip()
            if owner not in valid_owner:
                out['owner not an active employee'] += 1
                continue
            if ('Lead', lead.id) in already:
                out['already has an open task'] += 1
                continue

            stage = (lead.stage or '').strip()
            key = wanted.get(stage)
            if not key:
                out[f'no task for stage: {stage or "(blank)"}'] += 1
                continue
            # Don't raise a handover task for something already handed over,
            # nor a debrief where the competitor is already recorded.
            if key == 'deal.won.handoff' and _has_handover(lead.id):
                out['won: handover already exists'] += 1
                continue
            if key == 'deal.lost.debrief' and _has_competitor(lead.id):
                out['lost: competitor already logged'] += 1
                continue

            tdef = defs.get(key)
            if tdef is None:
                out[f'definition missing: {key}'] += 1
                continue

            due = _due_for(lead, tdef.sla_hours)
            route = (tdef.action_route or '').replace('{entity_id}', str(lead.id))
            out[key] += 1
            out['OVERDUE on arrival' if due < now else 'due in future'] += 1
            created.append((lead, key, owner, due, route, tdef))

        if not args.apply:
            for lead, key, owner, due, route, tdef in created[:25]:
                flag = 'OVERDUE' if due < now else ''
                print('%-7s %-26s %-20s %-11s %-19s %s' % (
                    lead.id, (lead.company or '')[:26], key, owner,
                    str(due)[:19], flag))
            if len(created) > 25:
                print(f'   ... and {len(created) - 25} more')
        else:
            for lead, key, owner, due, route, tdef in created:
                db.session.add(TaskInstance(
                    task_key=key,
                    entity_type='Lead',
                    entity_id=lead.id,
                    entity_display=(lead.company or '')[:120],
                    owner_user_id=owner,
                    owner_role=(tdef.allowed_roles or [None])[0],
                    status=TaskInstanceStatus.PENDING,
                    priority=tdef.priority or 3,
                    action_route=route or None,
                    created_at=now,
                    due_at=due,
                    notes=f'{MARKER} stage at seeding: {lead.stage}',
                ))
            db.session.commit()
            print(f'created {len(created)} tasks.')

        print('\n--- summary ---')
        for k, n in out.most_common():
            print(f'  {n:>5}  {k}')
        if not args.apply:
            print('\nPreview only.')
            print('  --apply                 create these tasks')
            print('  --apply --include-closed  also Won handover / Lost debrief')
            print('  --undo --apply          remove them again')


if __name__ == '__main__':
    main()
