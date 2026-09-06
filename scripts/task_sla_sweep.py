"""
Task SLA Sweeper — Phase 5 (CRM).

Adapted from Procam-lr-main/scripts/task_sla_sweep.py.  The only
adaptations are the app-import shape (CRM has no create_app factory)
and the removed dependency on TMS's app.extensions.

Run every 15 min (systemd timer / cron).  For every OPEN TaskInstance
whose due_at is in the past:
    * Mark status = 'Escalated'
    * Duplicate to the escalation_role from the TaskDefinition (if any).
    * Bump priority so it floats to the top of My Work.

For every OPEN TaskInstance about to breach SLA (< 2 h to go):
    * Bump priority by 1 (min 1).

Idempotent — reruns are safe.
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
        dupes = 0
        for inst in overdue:
            inst.status = TaskInstanceStatus.ESCALATED
            inst.priority = 1
            escalated += 1
            tdef = TaskDefinition.query.filter_by(task_key=inst.task_key).first()
            if tdef and tdef.escalation_role:
                existing = (TaskInstance.query
                            .filter(TaskInstance.task_key == inst.task_key,
                                    TaskInstance.entity_type == inst.entity_type,
                                    TaskInstance.entity_id == inst.entity_id,
                                    TaskInstance.owner_role == tdef.escalation_role,
                                    TaskInstance.status.in_(TaskInstanceStatus.OPEN))
                            .first())
                if not existing:
                    db.session.add(TaskInstance(
                        task_key=inst.task_key + '.escalated',
                        entity_type=inst.entity_type,
                        entity_id=inst.entity_id,
                        entity_display=inst.entity_display,
                        owner_role=tdef.escalation_role,
                        status=TaskInstanceStatus.PENDING,
                        priority=1,
                        action_route=inst.action_route,
                        due_at=now + timedelta(hours=4),
                        notes=f'Escalated from {inst.task_key} (SLA breach)',
                    ))
                    dupes += 1

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
        print(f'  [OK] escalated={escalated} · dupes_created={dupes} '
              f'· priority_bumped={len(soon_q)}')
        if args.verbose:
            for inst in overdue:
                print(f'    - {inst.task_key} {inst.entity_type}#{inst.entity_id} '
                      f'(was due {inst.due_at})')
        print('Done.')


if __name__ == '__main__':
    main()
