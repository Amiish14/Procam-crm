"""Give two immortal tasks a way to close. DRY RUN BY DEFAULT.

CRM-FINAL-CLOSEOUT-01 §10 — fault 3 of
docs/operations/RELEASE_2026_10_TASKS.md.

    .venv/bin/python scripts/2026_11_08_task_close_events.py --check
    .venv/bin/python scripts/2026_11_08_task_close_events.py --apply

WHAT WAS WRONG

`lead.qualify` waits on `Lead / Profile Sent`. **That stage no longer
exists** — `stage_map.ALIASES` retired it onto Call Done, so nothing
writes it and the task can never complete. The October release note
records it as "closes on Profile Sent ✓", which was true the day it
was written and stopped being true when the stage was renamed.

`lead.rfq_review` waits on `Lead / Quoted`. That stage exists, but a
lead is not obliged to visit it: RFQ Generated → Under Negotiation
happens when the customer negotiates off a verbal, and RFQ Generated
→ Lost happens when they go elsewhere. Either way the task stays
open on a lead nobody is working.

Both are the same shape of fault: one exact state name, and a lead
under no obligation to pass through it.

WHAT THIS DOES, IN ONE TRANSACTION

  1. rewrites both `completion_state` rules to name **every state at
     or beyond the point the task's work belongs to, plus the
     terminal states** — computed from `stage_map.JOURNEY` and
     `stage_map.TERMINAL`, never typed out, because a typed list is
     how this broke the first time;

  2. closes the rows that are already satisfied: open instances whose
     lead has long since moved past the point.

Step 1 without step 2 leaves the existing backlog open forever —
`on_state_change` only fires on the next transition, and these leads
have already had theirs. Step 2 without step 1 closes the backlog and
the engine immediately starts making more. They belong in one run.

COMPLETED OR CANCELLED, AND WHY BOTH

Where the lead advanced through the journey the work happened — a
lead cannot reach Quoted without its RFQ having been reviewed — and
the row is marked **Completed**. Where the lead died the work became
moot and nobody did it, so the row is **Cancelled**. Marking them all
Completed records work nobody did; marking them all Cancelled erases
work that was done. Each row's `notes` says which and why, and each
gets an audit row.

WHAT IT WILL NOT DO

It will not touch any task definition other than the two named. It
will not close a row whose lead is still at or before the point —
those are real open work.

`deal.lost.debrief` and `competitor.log`, the other two immortals in
fault 3, are **not** fixed here. They are a different problem: one
has no completion state at all and the other waits on an Opportunity
state that was never built, so each needs a decision about what
"done" means rather than a wider list of stages. They are counted in
the output so the remaining backlog is honest.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import Lead, app, db                              # noqa: E402
from app.models.task_engine import (TaskDefinition,        # noqa: E402
                                    TaskInstance,
                                    TaskInstanceStatus)
from app.services import task_close_rules as rules         # noqa: E402

ACTOR = 'FINAL-CLOSEOUT-01'

#: The other two immortals from fault 3. Counted, never touched —
#: each needs a decision about what "done" means, not a longer list.
LEFT_FOR_A_DECISION = ('deal.lost.debrief', 'competitor.log')


def definition_plan():
    """What each of the two definitions says now, and should say."""
    out = []
    for key in sorted(rules.AFTER):
        row = TaskDefinition.query.filter_by(task_key=key).first()
        wanted = rules.completion_rule(key)
        out.append({
            'key': key,
            'row': row,
            'now': (row.completion_state if row is not None else None),
            'wanted': wanted,
            'changes': (row is not None
                        and row.completion_state != wanted),
        })
    return out


def instance_plan():
    """Open rows whose lead has already passed the closing point."""
    leads = {l.id: l for l in Lead.query.all()}
    rows = (TaskInstance.query
            .filter(TaskInstance.task_key.in_(sorted(rules.AFTER)),
                    TaskInstance.entity_type == 'Lead',
                    TaskInstance.status.in_(TaskInstanceStatus.OPEN))
            .all())
    plan, staying = [], []
    for row in rows:
        lead = leads.get(row.entity_id)
        if lead is None:
            # No lead behind it. Not this script's business: the
            # backlog report's rules already cover orphans, and
            # guessing here would close a row for a reason that is
            # not the one this script is about.
            staying.append((row, 'no lead behind it'))
            continue
        if not rules.closes_now(row.task_key, lead.stage):
            staying.append((row, f'the lead is still on '
                                 f'{lead.stage or "no stage"}'))
            continue
        status, why = rules.outcome_for(lead.stage)
        plan.append({'row': row, 'lead': lead, 'status': status,
                     'why': why})
    return plan, staying


def report(defs, plan, staying):
    print('\n── the two completion rules ──')
    for item in defs:
        key = item['key']
        if item['row'] is None:
            print(f'  !! {key} has no definition row at all — nothing '
                  f'to fix, and nothing creating it either.')
            continue
        now = item['now'] or {}
        shown = (now.get('state')
                 or ', '.join(now.get('states') or [])
                 or 'nothing at all')
        print(f'  {key}')
        print(f'    waits on : {shown}')
        if key in rules.WAS and now.get('state') == rules.WAS[key]:
            if key == 'lead.qualify':
                print('               — and that stage was retired, so '
                      'this can never close')
            else:
                print('               — which a lead is not obliged to '
                      'pass through')
        print(f'    will close on: '
              f'{", ".join((item["wanted"] or {}).get("states") or [])}')
        if not item['changes']:
            print('    already correct')

    print('\n── rows already satisfied ──')
    by = Counter((p['row'].task_key, p['status']) for p in plan)
    if not plan:
        print('  none.')
    for (key, status), n in sorted(by.items()):
        print(f'  {n:>5}  {key:<20} → {status}')
    for p in plan[:12]:
        row, lead = p['row'], p['lead']
        print(f'    #{row.id:<6} {row.task_key:<18} lead #{lead.id} '
              f'({lead.stage}) → {p["status"]}')
    if len(plan) > 12:
        print(f'    … and {len(plan) - 12} more')

    if staying:
        print(f'\n── left open ({len(staying)}) ──')
        for reason, n in Counter(why for _row, why
                                 in staying).most_common(8):
            print(f'  {n:>5}  {reason}')

    print('\n── fault 3, the part this does not fix ──')
    for key in LEFT_FOR_A_DECISION:
        n = (TaskInstance.query
             .filter(TaskInstance.task_key == key,
                     TaskInstance.status.in_(TaskInstanceStatus.OPEN))
             .count())
        row = TaskDefinition.query.filter_by(task_key=key).first()
        what = ((row.completion_state or {}).get('state')
                if row is not None else None)
        print(f'  {n:>5}  {key:<22} waits on '
              f'{what or "nothing at all"} — needs a decision about '
              f'what "done" means, not a longer list of stages')
    return plan


def apply(defs, plan):
    from app.services import audit

    now = datetime.utcnow()
    changed_defs, closed = [], 0
    try:
        for item in defs:
            if not item['changes']:
                continue
            row = item['row']
            was = row.completion_state
            row.completion_state = item['wanted']
            row.updated_at = now
            audit.record('task_definition.completion_changed',
                         'task_definition', str(row.id),
                         old={'completion_state': was},
                         new={'completion_state': item['wanted']},
                         actor=ACTOR,
                         reason=(f'{item["key"]} could not close: it '
                                 f'waited on a single state a lead is '
                                 f'not obliged to reach'))
            changed_defs.append(item['key'])

        for p in plan:
            row = p['row']
            was = row.status
            row.status = p['status']
            row.completed_at = now
            note = (f'Closed by CRM-FINAL-CLOSEOUT-01 §10: {p["why"]}.')
            row.notes = ((row.notes + '\n') if row.notes else '') + note
            audit.record('task.close_event_backfilled', 'task_instance',
                         str(row.id),
                         old={'status': was},
                         new={'status': p['status'],
                              'lead_id': p['lead'].id,
                              'lead_stage': p['lead'].stage},
                         actor=ACTOR, reason=note[:400])
            closed += 1
        db.session.commit()
    except Exception as exc:                              # noqa: BLE001
        db.session.rollback()
        print(f'\n  !! ROLLED BACK: {type(exc).__name__}: '
              f'{str(exc)[:200]}')
        raise SystemExit(1)

    print(f'\n{len(changed_defs)} definition(s) changed: '
          f'{", ".join(changed_defs) or "none"}')
    print(f'{closed} row(s) closed, in one transaction, each with an '
          f'audit row and a note saying why.')
    left = (TaskInstance.query
            .filter(TaskInstance.status.in_(TaskInstanceStatus.OPEN))
            .count())
    print(f'open tasks now: {left}')
    print('From here the engine closes these itself: both rules now '
          'name every state at or beyond the point the work belongs '
          'to, so a lead that advances or dies closes its task in the '
          'same transaction.')
    return changed_defs, closed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true',
                    help='report only. The default.')
    ap.add_argument('--apply', action='store_true',
                    help='rewrite the two rules and close the rows '
                         'already satisfied')
    args = ap.parse_args(argv)

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('CRM-FINAL-CLOSEOUT-01 §10 — fault 3, the two tasks that '
              'could not close.'
              + ('' if args.apply else ' REPORT ONLY.'))

        defs = definition_plan()
        plan, staying = instance_plan()
        report(defs, plan, staying)

        if not args.apply:
            print('\nDRY RUN — nothing written. --apply rewrites the '
                  'two rules and closes the rows above.')
            db.session.rollback()
            return 0

        apply(defs, plan)
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
