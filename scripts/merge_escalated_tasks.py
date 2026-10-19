"""
Fold every '.escalated' twin back into the task it came from.

    .venv/bin/python scripts/merge_escalated_tasks.py --check
    .venv/bin/python scripts/merge_escalated_tasks.py --apply

The SLA sweeper used to escalate a task by creating a second row —
same entity, task_key '<key>.escalated', owned by the escalation role.
Three things were wrong with that:

  * it doubled every task count on every screen, so 3,659 tasks read
    as 7,318;
  * the twin could never be closed. Tasks close by matching a
    TaskDefinition's completion_state, and '<key>.escalated' matches
    no definition, so each twin was immortal from the moment it was
    written;
  * an escalation is something that happened to a task, not a second
    task. Two rows meant two owners could each think the other had it.

So the twin's facts move onto the original — `escalated`,
`escalated_to`, `escalated_at` — and the twin is removed. One
transaction, one audit row per merge, and the original keeps its id,
its history and its owner.

A twin with no surviving original is left alone and listed: deleting
it would lose the only record that the work was ever raised, and
there is nothing to merge it into.
"""
import argparse
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import app, db                                   # noqa: E402
from app.models.task_engine import (TaskInstance,         # noqa: E402
                                    TaskInstanceStatus)

SUFFIX = '.escalated'


def pairs():
    """[(twin, original)] and [orphan twins].

    The original is matched on the three things that identify a task:
    its key, its entity, and nothing else — the twin deliberately has
    a different owner, which is the whole point of an escalation.
    """
    twins = (TaskInstance.query
             .filter(TaskInstance.task_key.like(f'%{SUFFIX}'))
             .order_by(TaskInstance.id.asc()).all())
    matched, orphans = [], []
    for twin in twins:
        origin_key = twin.task_key[:-len(SUFFIX)]
        original = (TaskInstance.query
                    .filter(TaskInstance.task_key == origin_key,
                            TaskInstance.entity_type == twin.entity_type,
                            TaskInstance.entity_id == twin.entity_id)
                    .order_by(TaskInstance.id.asc()).first())
        (matched if original is not None else orphans).append(
            (twin, original) if original is not None else twin)
    return matched, orphans


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    if args.apply == args.check:
        raise SystemExit('Choose one: --check or --apply.')

    with app.app_context():
        print(f'database: {db.engine.url}')
        print(f'MODE:     {"APPLY" if args.apply else "DRY RUN — nothing is written"}\n')

        matched, orphans = pairs()
        total = TaskInstance.query.count()
        open_now = TaskInstance.query.filter(
            TaskInstance.status.in_(TaskInstanceStatus.OPEN)).count()

        print(f'  task rows now                 {total:>6}')
        print(f'  of them open                  {open_now:>6}')
        print(f'  twins to merge away           {len(matched):>6}')
        print(f'  twins with no original        {len(orphans):>6}')
        print(f'  task rows after               {total - len(matched):>6}')

        if matched:
            print('\n  by task:')
            for key, n in Counter(
                    t.task_key for t, _o in matched).most_common():
                print(f'      {n:>5}  {key}')
            print('\n  escalated to:')
            for role, n in Counter(
                    (t.owner_role or '(nobody)')
                    for t, _o in matched).most_common():
                print(f'      {n:>5}  {role}')
            already = sum(1 for _t, o in matched if o.escalated)
            print(f'\n  originals already flagged escalated: {already}')

        if orphans:
            print('\n  twins with no original — left alone:')
            for t in orphans[:10]:
                print(f'      #{t.id} {t.task_key} '
                      f'{t.entity_type}#{t.entity_id}')

        if args.check:
            print('\nDRY RUN — nothing written. Re-run with --apply.')
            return

        from app.services import audit
        try:
            for twin, original in matched:
                original.escalated = True
                original.escalated_to = (original.escalated_to
                                         or twin.owner_role)
                original.escalated_at = (original.escalated_at
                                         or twin.created_at)
                audit.record(
                    'task.escalation_merged', 'task_instance',
                    str(original.id),
                    old={'twin_id': twin.id, 'twin_key': twin.task_key,
                         'twin_owner_role': twin.owner_role,
                         'twin_status': twin.status},
                    new={'escalated': True,
                         'escalated_to': original.escalated_to,
                         'escalated_at': str(original.escalated_at)},
                    actor='CRM-TASKS-02',
                    reason='Escalation folded onto the task it came from; '
                           'the duplicate row was removed.')
                db.session.delete(twin)
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'\n  !! ROLLED BACK: {type(exc).__name__}: '
                  f'{str(exc)[:200]}')
            raise SystemExit(1)

        print(f'\n  {len(matched)} twin(s) merged and removed, in one '
              f'transaction, each with an audit row naming the row that '
              f'went. {len(orphans)} orphan(s) untouched.')


if __name__ == '__main__':
    main()
