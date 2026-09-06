"""PIC 360 — a person's commercial workload and performance (§22).

"A manager clicking any Procam PIC should see the person's authorised
commercial workload and performance."

Authorised is the operative word: this reads the same data scope as the
reports, so a vertical head sees the people in their vertical and an admin
sees everyone.  Nobody gains visibility here that they would be refused
elsewhere.
"""
from datetime import datetime, timedelta

from app import db


def visible_codes():
    """Employee codes the signed-in user may look at.

    None means unrestricted.  Mirrors the reports' data scope so the two
    cannot disagree about who is visible.
    """
    from app.access.service import data_scope
    from app.models.access import DataScope
    from flask import session
    from app import Employee
    from sqlalchemy import or_

    scope = data_scope()
    if scope == DataScope.ALL:
        return None

    me = Employee.query.filter_by(emp_code=session.get('emp_code')).first()
    if me is None:
        return set()
    if scope == DataScope.OWN:
        return {me.emp_code}

    vertical = (me.vertical or '').strip()
    codes = {me.emp_code}
    clauses = [Employee.vertical_head_id == me.id]
    if vertical:
        clauses.append(Employee.vertical == vertical)
    for e in Employee.query.filter(or_(*clauses)).all():
        if e.emp_code:
            codes.add(e.emp_code)
    return codes


def may_view(emp_code):
    allowed = visible_codes()
    return allowed is None or emp_code in allowed


def workload(emp_code):
    """§22 — what this person is carrying right now."""
    from app import Lead, Opportunity, Company, Contact

    leads = Lead.query.filter(Lead.assigned_to == emp_code)
    opps = Opportunity.query.filter(Opportunity.owner_emp_code == emp_code)
    won = opps.filter(Opportunity.stage == 'Won').all()
    lost = opps.filter(Opportunity.stage == 'Lost').count()
    won_value = sum(float(o.value_inr or 0) for o in won)
    decided = len(won) + lost

    open_tasks = overdue = completed = 0
    sla_met = sla_total = 0
    try:
        from app.models.task_engine import (TaskInstance, TaskDefinition,
                                            TaskInstanceStatus)
        mine = TaskInstance.query.filter(
            TaskInstance.owner_user_id == emp_code)
        open_q = mine.filter(TaskInstance.status.in_(TaskInstanceStatus.OPEN))
        open_tasks = open_q.count()
        overdue = open_q.filter(TaskInstance.due_at.isnot(None),
                                TaskInstance.due_at < datetime.utcnow()).count()
        done = mine.filter(
            TaskInstance.status == TaskInstanceStatus.COMPLETED).all()
        completed = len(done)

        slas = {d.task_key: d.sla_hours for d in TaskDefinition.query.all()}
        for t in done:
            hours = slas.get(t.task_key)
            if not hours or not t.created_at or not t.completed_at:
                continue
            sla_total += 1
            elapsed = (t.completed_at - t.created_at).total_seconds() / 3600.0
            if elapsed <= hours:
                sla_met += 1
    except Exception:
        pass

    def _count(path, column):
        import importlib
        module_path, _, attr = path.partition(':')
        try:
            model = getattr(importlib.import_module(module_path), attr)
            return model.query.filter(getattr(model, column) == emp_code).count()
        except Exception:
            return 0

    return {
        'leads': leads.count(),
        'open_leads': leads.filter(~Lead.stage.in_(['Won', 'Lost'])).count(),
        'accounts': Company.query.filter(
            Company.pic_emp_code == emp_code,
            Company.is_active.is_(True)).count(),
        'contacts': Contact.query.filter(
            Contact.assigned_to == emp_code).count(),
        'opportunities': opps.count(),
        'won': len(won),
        'won_value': round(won_value, 2),
        'lost': lost,
        'win_rate': round(len(won) / decided * 100, 1) if decided else None,
        'rfqs': _count('app.models.rfq:RFQ', 'lead_driver'),
        'quotes': _count('app.models.quote:Quote', 'prepared_by_id'),
        'open_tasks': open_tasks,
        'overdue_tasks': overdue,
        'completed_tasks': completed,
        'sla_hit': round(sla_met / sla_total * 100, 1) if sla_total else None,
        'sla_sample': sla_total,
    }


def recent_activity(emp_code, days=30):
    from app import LeadActivity
    since = datetime.utcnow() - timedelta(days=days)
    rows = (LeadActivity.query
            .filter(LeadActivity.performed_by == emp_code,
                    LeadActivity.occurred_at >= since)
            .order_by(LeadActivity.occurred_at.desc()).limit(50).all())
    return [{
        'at': str(a.occurred_at)[:16] if a.occurred_at else '',
        'kind': (a.kind or 'note').title(),
        'subject': a.subject or '',
        'lead_id': a.lead_id,
    } for a in rows]


def open_tasks(emp_code, limit=40):
    try:
        from app.models.task_engine import TaskInstance, TaskInstanceStatus
    except Exception:
        return []
    rows = (TaskInstance.query
            .filter(TaskInstance.owner_user_id == emp_code,
                    TaskInstance.status.in_(TaskInstanceStatus.OPEN))
            .order_by(TaskInstance.due_at.asc().nullslast())
            .limit(limit).all())
    now = datetime.utcnow()
    return [{
        'id': t.id, 'task_key': t.task_key,
        'entity': t.entity_display or f'{t.entity_type}#{t.entity_id}',
        'status': t.status, 'priority': t.priority,
        'due_at': str(t.due_at)[:16] if t.due_at else '',
        'overdue': bool(t.due_at and t.due_at < now),
        'route': t.action_route or '',
    } for t in rows]


def accounts(emp_code, limit=60):
    from app import Company
    rows = (Company.query
            .filter(Company.pic_emp_code == emp_code,
                    Company.is_active.is_(True))
            .order_by(Company.name).limit(limit).all())
    return [{'id': c.id, 'name': c.name,
             'stage': c.dev_stage or '',
             'last_activity_at': str(c.last_activity_at)[:10]
                                 if c.last_activity_at else ''} for c in rows]


def full(employee):
    return {
        'employee': {
            'emp_code': employee.emp_code, 'name': employee.name,
            'designation': employee.designation or '',
            'department': employee.department or '',
            'vertical': employee.vertical or '',
            'role': employee.role or '',
            'email': employee.email or '',
            'is_active': bool(employee.is_active),
            'is_vertical_head': bool(getattr(employee, 'is_vertical_head',
                                             False)),
        },
        'workload': workload(employee.emp_code),
        'tasks': open_tasks(employee.emp_code),
        'accounts': accounts(employee.emp_code),
        'activity': recent_activity(employee.emp_code),
    }
