"""
What one person should do next, read from the engines that already
decide it.

CRM 2.0 R3.

No second engine
    Every part of this is somebody else's answer, fetched:

      my next action   the Workbench's own `lead_items`, which is
                       where the conditions, priorities and due dates
                       are already decided (app/workbench/service.py)
      overdue          the same list, split into leads and tasks the
                       way the Overview tiles split them
      escalated to me  `TaskInstance.escalated` / `escalated_to`,
                       the three columns that replaced the
                       `.escalated` twin rows

    That constraint is the point of R3 rather than a note on it. The
    last time this CRM grew a second opinion about tasks it created a
    twin row per task, doubled every count on every screen, and the
    twins could never be closed because `<key>.escalated` matches no
    TaskDefinition. A cockpit with its own idea of "overdue" would be
    the same mistake with a nicer surface.

What it adds
    Only the arrangement: one person's work in the order they should
    look at it, and the three questions they actually ask — what now,
    what is late, what has been pushed at me.
"""
from __future__ import annotations

from datetime import datetime

#: Enough to act on, few enough to read. The Workbench is where
#: somebody goes to see all of it.
TOP_N = 8


def _open_tasks_for(emp_code):
    from app.models.task_engine import TaskInstance, TaskInstanceStatus

    if not emp_code:
        return []
    return (TaskInstance.query
            .filter(TaskInstance.owner_user_id == emp_code,
                    TaskInstance.status.in_(TaskInstanceStatus.OPEN))
            .order_by(TaskInstance.due_at.asc().nullslast(),
                      TaskInstance.priority.asc())
            .all())


def _escalated_to(sc, emp_code):
    """Tasks escalated to this person, or to a role they hold.

    `escalated_to` holds a role, not an employee code — the ladder in
    `escalation.py` escalates to whoever holds a role, and who that is
    changes. So the role is resolved now rather than being copied onto
    the row when it fired.
    """
    from app.models.task_engine import TaskInstance, TaskInstanceStatus

    roles = set()
    for perm in ('admin.triage', 'admin.master'):
        if sc is not None and sc.can(perm):
            roles.add(perm)
    q = (TaskInstance.query
         .filter(TaskInstance.escalated.is_(True),
                 TaskInstance.status.in_(TaskInstanceStatus.OPEN)))
    rows = q.order_by(TaskInstance.escalated_at.desc()).all()

    mine = []
    for t in rows:
        to = (t.escalated_to or '').strip()
        if t.owner_user_id == emp_code:
            mine.append((t, 'your task, escalated'))
        elif to and (to in roles or to.upper() == (emp_code or '').upper()):
            mine.append((t, f'escalated to {to}'))
    return mine


def _task_row(task, why=''):
    overdue = bool(task.due_at and task.due_at < datetime.utcnow())
    return {
        'kind': 'task',
        'id': task.id,
        'title': task.entity_display or task.task_key,
        'task_key': task.task_key,
        'entity_type': task.entity_type,
        'entity_id': task.entity_id,
        'due': task.due_at.isoformat(timespec='seconds')
               if task.due_at else None,
        'overdue': overdue,
        'escalated': bool(task.escalated),
        'escalated_to': task.escalated_to or '',
        'route': task.action_route or '',
        'why': why,
    }


def mine_only(sc, emp_code):
    """The viewer's own book, inside whatever they may already see.

    An administrator's scope is everything, and a cockpit that opens
    on 945 leads is a report, not a cockpit. This narrows to the one
    person the same way the Workbench narrows for its `?for=`
    parameter — by building a new Scope rather than filtering after
    the fact, so nothing downstream can widen it again.
    """
    from app.access import scope as sc_mod

    if sc is None or not emp_code:
        return sc
    return sc_mod.Scope(sc.emp_code, sc.vertical, {emp_code},
                        sc.perms, sc.data_scope)


def for_person(sc, emp_code, *, today=None):
    """The three questions, answered from the existing engines."""
    from app.services import sales_rules as rules
    from app.workbench import filters as wb_filters
    from app.workbench import service as wb

    today = today or rules.business_today()
    ctx = {'today': today}
    items, _ageing = wb.lead_items(mine_only(sc, emp_code),
                                   today=today, include_quiet=True)
    live = [i for i in items
            if i.get('group') != wb.G_NOTHING_OUTSTANDING]

    # The Workbench has already sorted by priority and due date; the
    # cockpit does not get its own opinion about the order.
    mine = live

    tasks = _open_tasks_for(emp_code)
    overdue_tasks = [t for t in tasks
                     if t.due_at and t.due_at < datetime.utcnow()]
    overdue_leads = [
        i for i in live
        if wb_filters.BY_KEY['overdue_leads'].predicate(i, ctx)]

    escalated = _escalated_to(sc, emp_code)

    to_confirm = [i for i in items if wb_filters.needs_confirming(i)]

    return {
        'next_action': mine[:TOP_N],
        'next_action_total': len(mine),
        'overdue_leads': overdue_leads[:TOP_N],
        'overdue_leads_total': len(overdue_leads),
        'overdue_tasks': [_task_row(t) for t in overdue_tasks[:TOP_N]],
        'overdue_tasks_total': len(overdue_tasks),
        'escalated': [_task_row(t, why) for t, why in escalated[:TOP_N]],
        'escalated_total': len(escalated),
        'open_tasks_total': len(tasks),
        'to_confirm': to_confirm[:TOP_N],
        'to_confirm_total': len(to_confirm),
    }
