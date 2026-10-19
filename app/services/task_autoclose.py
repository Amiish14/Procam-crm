"""
When a lead is closed, its open tasks close with it.

CRM-AUTOCLOSE-TASKS-01.

Why this is needed at all
    Tasks close by matching a TaskDefinition's `completion_state`.
    Not one of the twenty definitions names Won, Lost, On Hold or Not
    Interested, so a lead reaching the end of its life closed nothing
    behind it. On 19 October, 2,262 of 2,694 open Lead tasks sat on a
    lead that was already Won, Lost, On Hold or Not Interested —
    work nobody could do, about deals nobody was working, counted on
    every screen that counts tasks.

    Chasing a quote on a lead that was won three weeks ago is not a
    task. It is noise that makes the real tasks harder to see.

What it does
    Marks every open task on that lead `Cancelled` — not `Completed`,
    because nobody completed them. The reason goes in `notes` so the
    row still says why it stopped, and `completed_at` records when.
    One audit row per task.

    It runs in the caller's transaction and does not commit. The
    close and the tasks that close with it land together or not at
    all, which is the point: a lead marked Won with its tasks still
    open is the state this exists to prevent.
"""
from __future__ import annotations

from datetime import datetime

from app import db
from app.models.task_engine import TaskInstance, TaskInstanceStatus

#: Lead stages that mean the work is over, win or lose.
TERMINAL_STAGES = ('Won', 'Lost', 'On Hold', 'Not Interested')

NOTE = 'Closed automatically: the lead reached {stage}.'


def is_terminal(stage):
    return (stage or '') in TERMINAL_STAGES


def open_tasks_for(lead_id, entity_type='Lead'):
    return (TaskInstance.query
            .filter(TaskInstance.entity_type == entity_type,
                    TaskInstance.entity_id == lead_id,
                    TaskInstance.status.in_(TaskInstanceStatus.OPEN))
            .order_by(TaskInstance.id.asc()).all())


def close_for_lead(lead, actor=None, reason=None):
    """Cancel the open tasks on a closed lead. Returns how many.

    Does nothing — and says so by returning 0 — when the lead is not
    in a terminal stage. Callers fire it on every save rather than
    working out whether they should, so the rule lives here and not
    in five routes.
    """
    if lead is None or not is_terminal(getattr(lead, 'stage', None)):
        return 0
    rows = open_tasks_for(lead.id)
    if not rows:
        return 0

    from app.services import audit
    now = datetime.utcnow()
    note = reason or NOTE.format(stage=lead.stage)
    for row in rows:
        was = row.status
        row.status = TaskInstanceStatus.CANCELLED
        row.completed_at = now
        if actor:
            row.completed_by_id = actor
        row.notes = ((row.notes + '\n') if row.notes else '') + note
        try:
            audit.record('task.closed_with_lead', 'task_instance',
                         str(row.id),
                         old={'status': was},
                         new={'status': TaskInstanceStatus.CANCELLED,
                              'lead_id': lead.id, 'stage': lead.stage},
                         actor=actor or 'system', reason=note[:200])
        except Exception:                                 # noqa: BLE001
            # An audit row that will not write must not keep a task
            # open. The cancellation is the thing being asked for.
            pass
    return len(rows)
