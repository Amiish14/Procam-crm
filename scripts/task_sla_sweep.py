"""
Task SLA Sweeper — Phase 5 (CRM).

Adapted from Procam-lr-main/scripts/task_sla_sweep.py.  The only
adaptations are the app-import shape (CRM has no create_app factory)
and the removed dependency on TMS's app.extensions.

Run every 15 min (systemd timer / cron).  For every OPEN TaskInstance
whose due_at is in the past:
    * Mark status = 'Escalated'
    * Record the escalation ON THE TASK: escalated, escalated_to,
      escalated_at.
    * Bump priority so it floats to the top of My Work.

For every OPEN TaskInstance about to breach SLA (< 2 h to go):
    * Bump priority by 1 (min 1).

Idempotent — reruns are safe.

2026-10-19. This used to escalate by writing a *second* row with
task_key '<key>.escalated' owned by the escalation role. It produced
3,659 duplicates, it doubled every task count on every screen, and
not one of them could ever be closed: tasks close by matching a
TaskDefinition's completion_state, and '<key>.escalated' matches no
definition. An escalation is something that happened to a task, not
a second task, so it is now three columns on the original.

The duplicate check was also looking for the wrong thing — it
searched for a row with the *original* key and the escalation role,
while creating one with the '.escalated' key, so it could never find
what it had made. It only avoided running away because the outer
loop reads Pending and the row it had just escalated was no longer
Pending.
"""
import os
import sys
import argparse
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import app, db                                                 # noqa: E402
from app.models.task_engine import (                                    # noqa: E402
    TaskDefinition, TaskInstance, TaskInstanceStatus,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--verbose', '-v', action='store_true')
    args = ap.parse_args()

    with app.app_context():
        now = datetime.utcnow()
        soon = now + timedelta(hours=2)

        # 1) Overdue → Escalated
        overdue = (TaskInstance.query
                   .filter(TaskInstance.status == TaskInstanceStatus.PENDING,
                           TaskInstance.due_at.isnot(None),
                           TaskInstance.due_at < now).all())
        escalated = 0
        flagged = 0
        for inst in overdue:
            inst.status = TaskInstanceStatus.ESCALATED
            inst.priority = 1
            escalated += 1
            tdef = TaskDefinition.query.filter_by(task_key=inst.task_key).first()
            if tdef and tdef.escalation_role and not inst.escalated:
                # On the task, not beside it. Re-running finds
                # `escalated` already true and does nothing, which is
                # what the old duplicate check was trying and failing
                # to do.
                inst.escalated = True
                inst.escalated_to = tdef.escalation_role
                inst.escalated_at = now
                flagged += 1

        # 2) About to breach → bump priority
        soon_q = (TaskInstance.query
                  .filter(TaskInstance.status == TaskInstanceStatus.PENDING,
                          TaskInstance.due_at.isnot(None),
                          TaskInstance.due_at >= now,
                          TaskInstance.due_at <= soon,
                          TaskInstance.priority > 1).all())
        for inst in soon_q:
            inst.priority = max(1, inst.priority - 1)

        db.session.commit()
        print(f'  [OK] escalated={escalated} · newly_flagged={flagged} '
              f'· priority_bumped={len(soon_q)}')
        if args.verbose:
            for inst in overdue:
                print(f'    - {inst.task_key} {inst.entity_type}#{inst.entity_id} '
                      f'(was due {inst.due_at})')
        print('Done.')


if __name__ == '__main__':
    main()
