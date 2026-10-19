"""
Close the four tasks named in CRM-AUTOCLOSE-TASKS-01.

    .venv/bin/python scripts/close_named_tasks.py --check
    .venv/bin/python scripts/close_named_tasks.py --apply

    1342  lead.rfq_review            Lead#11531  Siemens Energy India
    1541  lead.rfq_review.escalated  Lead#11531  (the twin)
    1283  deal.negotiation           Lead#11518  SIEMENS LIMITED
    1536  deal.negotiation.escalated Lead#11518  (the twin)

Both leads are Won. Both tasks have an escalated twin, which is the
pattern the merge script removes generally — these four are named
explicitly because they were asked for by id.

Cancelled rather than Completed: nobody did them. The reason is
written into `notes` so the row still says why it stopped, and each
gets an audit row. Running it twice closes nothing the second time.
"""
import argparse
import os
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402
from app.models.task_engine import (TaskInstance,         # noqa: E402
                                    TaskInstanceStatus)

TASK_IDS = (1342, 1541, 1283, 1536)
REASON = ('Closed on instruction (CRM-AUTOCLOSE-TASKS-01): the lead is '
          'already closed and the work is not outstanding.')


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

        todo, skip = [], []
        for tid in TASK_IDS:
            row = db.session.get(TaskInstance, tid)
            if row is None:
                skip.append((tid, 'no such task'))
                continue
            lead = (db.session.get(Lead, row.entity_id)
                    if row.entity_type == 'Lead' else None)
            stage = lead.stage if lead is not None else '(no lead)'
            label = (f'#{tid} {row.task_key:<28} '
                     f'{row.entity_type}#{row.entity_id} '
                     f'lead={stage:<16} status={row.status}')
            if row.status not in TaskInstanceStatus.OPEN:
                skip.append((tid, f'already {row.status}'))
                print(f'  skip  {label}')
                continue
            todo.append(row)
            print(f'  close {label}')

        print(f'\n  to close      {len(todo)}')
        print(f'  left alone    {len(skip)}')
        for tid, why in skip:
            print(f'      #{tid}: {why}')

        if args.check:
            print('\nDRY RUN — nothing written. Re-run with --apply.')
            return

        from app.services import audit
        now = datetime.utcnow()
        try:
            for row in todo:
                was = row.status
                row.status = TaskInstanceStatus.CANCELLED
                row.completed_at = now
                row.notes = ((row.notes + '\n') if row.notes else '') + REASON
                audit.record('task.closed_on_instruction', 'task_instance',
                             str(row.id), old={'status': was},
                             new={'status': TaskInstanceStatus.CANCELLED},
                             actor='CRM-AUTOCLOSE-TASKS-01',
                             reason=REASON[:200])
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'\n  !! ROLLED BACK: {type(exc).__name__}: '
                  f'{str(exc)[:200]}')
            raise SystemExit(1)
        print(f'\n  {len(todo)} task(s) closed, in one transaction.')


if __name__ == '__main__':
    main()
