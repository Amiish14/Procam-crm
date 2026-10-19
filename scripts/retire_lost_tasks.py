"""
Retire `deal.lost.debrief` and `competitor.log`.

    .venv/bin/python scripts/retire_lost_tasks.py --check
    .venv/bin/python scripts/retire_lost_tasks.py --apply

Two things, in one transaction:

  1. The definitions are deactivated, so no more are generated.
  2. The open instances are closed as
     'retired — captured in Lost dialog'.

Why they are going. Both were raised on every lost deal — 1,156 each
— and neither could ever be closed. `deal.lost.debrief` has no
completion state at all. `competitor.log` waits for an Opportunity to
reach 'Competitor Logged', which is not one of the six opportunity
stages, so it waits forever. Between them they were 2,312 of the
2,528 open tasks and the whole of the growth: a new pair every time
an opportunity is lost.

The questions they asked were the right ones. They are now two
optional fields on the Lost dialog, filled by the person closing the
deal at the moment they already know the answer — see
`app/services/lost_capture.py`.

Closed as `Cancelled`, not `Completed`: nobody did them. The reason
goes into each task's notes and into an audit row, so a row still
explains itself to whoever finds it later.
"""
import argparse
import os
import sys
from collections import Counter
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import app, db                                   # noqa: E402
from app.models.task_engine import (TaskDefinition,       # noqa: E402
                                    TaskInstance,
                                    TaskInstanceStatus)

RETIRE = ('deal.lost.debrief', 'competitor.log')

NOTE = ('Retired 19 October: this question is now asked in the Lost '
        'dialog (competitor and debrief note) instead of raised as a '
        'task. Not resent, not reassigned.')


def targets():
    """(definitions still active, open instances) for the two keys."""
    defs = [d for d in TaskDefinition.query.filter(
        TaskDefinition.task_key.in_(RETIRE)).all()]
    rows = (TaskInstance.query
            .filter(TaskInstance.task_key.in_(RETIRE),
                    TaskInstance.status.in_(TaskInstanceStatus.OPEN))
            .order_by(TaskInstance.id.asc()).all())
    return defs, rows


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

        defs, rows = targets()
        open_all = TaskInstance.query.filter(
            TaskInstance.status.in_(TaskInstanceStatus.OPEN)).count()

        print('  definitions to deactivate:')
        for d in defs:
            print(f'      {d.task_key:<22} active={d.is_active}  '
                  f'completion={d.completion_state}')
        if not defs:
            print('      (none found — already gone?)')

        print(f'\n  open tasks now                  {open_all:>6}')
        print(f'  to close                        {len(rows):>6}')
        print(f'  would remain open               {open_all - len(rows):>6}')

        if rows:
            print('\n  by task:')
            for k, n in Counter(r.task_key for r in rows).most_common():
                print(f'      {n:>5}  {k}')
            print('\n  by owner:')
            owners = Counter((r.owner_user_id or r.owner_role or '(nobody)')
                             for r in rows)
            for who, n in owners.most_common(10):
                print(f'      {n:>5}  {who}')
            if len(owners) > 10:
                print(f'      … and {len(owners) - 10} more')

        if args.check:
            print('\nDRY RUN — nothing written. Re-run with --apply.')
            return

        from app.services import audit
        now = datetime.utcnow()
        try:
            for d in defs:
                if d.is_active:
                    d.is_active = False
                    audit.record('task_definition.retired',
                                 'task_definition', str(d.id),
                                 old={'is_active': True},
                                 new={'is_active': False,
                                      'task_key': d.task_key},
                                 actor='CRM-TASKS-03', reason=NOTE[:200])
            for row in rows:
                was = row.status
                row.status = TaskInstanceStatus.CANCELLED
                row.completed_at = now
                row.notes = ((row.notes + '\n') if row.notes else '') + NOTE
                audit.record('task.retired', 'task_instance', str(row.id),
                             old={'status': was},
                             new={'status': TaskInstanceStatus.CANCELLED,
                                  'task_key': row.task_key},
                             actor='CRM-TASKS-03', reason=NOTE[:200])
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'\n  !! ROLLED BACK: {type(exc).__name__}: '
                  f'{str(exc)[:200]}')
            raise SystemExit(1)

        left = TaskInstance.query.filter(
            TaskInstance.status.in_(TaskInstanceStatus.OPEN)).count()
        print(f'\n  {len(defs)} definition(s) deactivated and '
              f'{len(rows)} task(s) closed, in one transaction, each '
              f'with an audit row.')
        print(f'  open tasks now: {left}')


if __name__ == '__main__':
    main()
