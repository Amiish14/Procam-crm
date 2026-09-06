"""
Task Routing Engine — runtime service (Phase 3 of the CRM upgrade).

Adapted from `Procam-lr-main/app/services/task_engine.py`.  Two
key adaptations for CRM:

  1. `from app.extensions import db` → `from app import db`.
  2. TMS's `users.id` (int) → CRM's `employees.emp_code` (str).  Every
     user identifier here is an emp_code.  The `.role` attribute on
     the acting user is `Employee.role` — same name.
  3. Owner-resolution for `role` / `primary_owner` calls out to
     `app.services.assignment` so tasks flow to the RBAC members
     of the record, falling back to the legacy owner.

Public API:

    on_state_change(entity, entity_type, old_state, new_state,
                    triggered_by=None)
        Called at the end of every route that transitions an entity
        state.  The engine:
          1. Closes any open TaskInstance whose completion_state
             matches (entity_type, new_state).
          2. Consults TaskDefinition where start_state matches.
          3. Resolves the owner via next_owner_rule.
          4. Inserts a new TaskInstance for the new owner.

    my_work(user)  → dict of 6 buckets for the current user.
    counts_for(user) → dict of 6 counters for the home / topbar tiles.

Idempotency: an open TaskInstance with matching (task_key,
entity_type, entity_id, owner_user_id or owner_role) is not created
twice.  All errors are caught and logged; the parent request never
fails because of task-engine work.
"""
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import and_, or_

from app import db
from app.models.task_engine import (
    TaskDefinition, TaskInstance, TaskInstanceStatus,
)


# =========================================================================
# STATE-CHANGE HOOK
# =========================================================================
def on_state_change(entity, entity_type, old_state, new_state,
                    triggered_by=None):
    """Central hook — fires after any route mutates entity state.

    Safe to call unconditionally.  If the engine can't find a matching
    task_definition, it does nothing.  Errors are logged and swallowed.
    """
    try:
        _close_matching_open_tasks(entity, entity_type, new_state,
                                   completed_by=triggered_by)
        _create_new_tasks_for(entity, entity_type, new_state)
        db.session.flush()
    except Exception as exc:
        try:
            current_app.logger.exception(
                'task_engine.on_state_change failed for %s#%s %s→%s: %s',
                entity_type, getattr(entity, 'id', '?'),
                old_state, new_state, exc)
        except Exception:
            pass


# =========================================================================
# RETURN / REWORK
# =========================================================================
def on_return_for_rework(entity, entity_type, return_reason,
                         triggered_by=None,
                         return_to_task_key=None,
                         return_to_owner_role=None):
    """Called when an approver rejects / sends-back an entity."""
    try:
        # Prefer the entity's legacy owner as the person to correct it.
        owner_user_id = None
        for attr in ('assigned_to', 'owner_emp_code', 'pic_emp_code',
                     'created_by'):
            v = getattr(entity, attr, None)
            if v:
                owner_user_id = str(v)
                break
        owner_role = return_to_owner_role or 'Lead_Driver'
        display = _entity_display(entity, entity_type)
        inst = TaskInstance(
            task_key=return_to_task_key or f'{entity_type.lower()}.rework',
            entity_type=entity_type, entity_id=entity.id,
            entity_display=display,
            owner_user_id=owner_user_id,
            owner_role=None if owner_user_id else owner_role,
            status=TaskInstanceStatus.RETURNED,
            priority=1,
            action_route=_default_detail_url(entity_type, entity.id),
            notes=return_reason,
            return_reason=return_reason,
        )
        db.session.add(inst)
        db.session.flush()
    except Exception as exc:
        try:
            current_app.logger.exception(
                'task_engine.on_return_for_rework failed: %s', exc)
        except Exception:
            pass


def _default_detail_url(entity_type, entity_id):
    mapping = {
        'Lead':        f'/app?lead={entity_id}',
        'Opportunity': f'/app?opp={entity_id}',
        'Company':     f'/app?account={entity_id}',
        'Account':     f'/app?account={entity_id}',
        'Project':     f'/app?project={entity_id}',
        'Quote':       f'/app?quote={entity_id}',
    }
    return mapping.get(entity_type, '/my-work')


def _close_matching_open_tasks(entity, entity_type, new_state,
                               completed_by=None):
    matching_defs = (TaskDefinition.query
                     .filter(TaskDefinition.is_active.is_(True))
                     .all())
    matches = [d for d in matching_defs
               if _state_matches(d.completion_state, entity_type, new_state)]
    if not matches:
        return
    keys = {d.task_key for d in matches}
    open_rows = (TaskInstance.query
                 .filter(TaskInstance.task_key.in_(keys),
                         TaskInstance.entity_type == entity_type,
                         TaskInstance.entity_id == entity.id,
                         TaskInstance.status.in_(TaskInstanceStatus.OPEN))
                 .all())
    now = datetime.utcnow()
    for row in open_rows:
        row.status = TaskInstanceStatus.COMPLETED
        row.completed_at = now
        if completed_by is not None:
            emp_code = getattr(completed_by, 'emp_code', None) \
                       or (completed_by if isinstance(completed_by, str) else None)
            if emp_code:
                row.completed_by_id = emp_code


def _create_new_tasks_for(entity, entity_type, new_state):
    all_defs = (TaskDefinition.query
                .filter(TaskDefinition.is_active.is_(True))
                .all())
    matches = [d for d in all_defs
               if _state_matches(d.start_state, entity_type, new_state)]
    for tdef in matches:
        owners = _resolve_owners(entity, tdef)
        for owner in owners:
            _upsert_task_instance(tdef, entity, entity_type, owner)


def _upsert_task_instance(tdef, entity, entity_type, owner):
    owner_user_id = owner.get('user_id')
    owner_role = owner.get('role')
    q = TaskInstance.query.filter(
        TaskInstance.task_key == tdef.task_key,
        TaskInstance.entity_type == entity_type,
        TaskInstance.entity_id == entity.id,
        TaskInstance.status.in_(TaskInstanceStatus.OPEN),
    )
    if owner_user_id:
        q = q.filter(TaskInstance.owner_user_id == owner_user_id)
    elif owner_role:
        q = q.filter(TaskInstance.owner_user_id.is_(None),
                     TaskInstance.owner_role == owner_role)
    existing = q.first()
    if existing:
        return existing

    display = _entity_display(entity, entity_type)
    action = _render_action_route(tdef.action_route, entity)
    due = None
    if tdef.sla_hours:
        due = datetime.utcnow() + timedelta(hours=tdef.sla_hours)

    inst = TaskInstance(
        task_key=tdef.task_key,
        entity_type=entity_type,
        entity_id=entity.id,
        entity_display=display,
        owner_user_id=owner_user_id,
        owner_role=owner_role,
        status=TaskInstanceStatus.PENDING,
        priority=tdef.priority or 3,
        action_route=action,
        due_at=due,
    )
    db.session.add(inst)
    # Emit a Notification for user-scoped tasks.  Role-pooled tasks
    # are skipped — they'd spam every user of that role.
    if owner_user_id:
        try:
            from app.models.notification import Notification, NotificationKind
            db.session.add(Notification(
                user_id=owner_user_id,
                kind=NotificationKind.TASK_ASSIGNED,
                title=f'New task: {tdef.title}',
                body=f'{entity_type} {display} needs your action.',
                entity_type=entity_type, entity_id=entity.id,
                action_url=action,
            ))
        except Exception:
            pass
    return inst


# =========================================================================
# OWNER RESOLUTION
# =========================================================================
def _resolve_owners(entity, tdef):
    """Return a list of {user_id, role} dicts for whom to create tasks."""
    rule = tdef.next_owner_rule or {}
    kind = rule.get('kind')

    if kind == 'role':
        role_key = rule.get('role')
        # Prefer an RBAC member of THIS record with that role.
        try:
            from app.services.assignment import members_of
            for m in members_of(entity, role_key=role_key):
                return [{'user_id': m.user_id, 'role': role_key}]
        except Exception:
            pass
        return [{'user_id': None, 'role': role_key}]

    if kind == 'role_any':
        return [{'user_id': None, 'role': r}
                for r in rule.get('roles', []) or []]

    if kind == 'primary_owner':
        try:
            from app.services.assignment import primary_owner_of
            uid = primary_owner_of(entity)
            if uid:
                return [{'user_id': uid, 'role': None}]
        except Exception:
            pass
        # Legacy fallback below
        for attr in ('assigned_to', 'owner_emp_code', 'pic_emp_code'):
            v = getattr(entity, attr, None)
            if v:
                return [{'user_id': str(v), 'role': None}]
        return []

    if kind == 'field':
        path = rule.get('path')
        v = getattr(entity, path, None) if path else None
        if v:
            return [{'user_id': str(v), 'role': None}]
        return []

    if kind == 'user':
        uid = rule.get('user_id')
        if uid:
            return [{'user_id': str(uid), 'role': None}]

    # Fallback — first allowed role (best-effort pool).
    if tdef.allowed_roles:
        return [{'user_id': None, 'role': tdef.allowed_roles[0]}]
    return []


# =========================================================================
# HELPERS
# =========================================================================
def _state_matches(rule, entity_type, state):
    """rule is a dict like {'entity': 'Lead', 'state': 'RFQ Generated'}.

    A rule with `state='*'` matches any state (useful for wildcards).
    A rule with `state='__created__'` fires on entity creation.
    """
    if not rule or not isinstance(rule, dict):
        return False
    if str(rule.get('entity', '')).lower() != str(entity_type).lower():
        return False
    r_state = str(rule.get('state', '')).lower()
    if r_state == '*':
        return True
    return r_state == str(state).lower()


def _entity_display(entity, entity_type):
    """Human-friendly label for the task instance."""
    for attr in ('opp_number', 'project_code', 'name', 'company'):
        v = getattr(entity, attr, None)
        if v:
            return str(v)[:120]
    return f'{entity_type}#{getattr(entity, "id", "?")}'


def _render_action_route(template, entity):
    if not template:
        return None
    try:
        return template.replace('{entity_id}', str(getattr(entity, 'id', '')))
    except Exception:
        return template


# =========================================================================
# MY WORK — queries for the /my-work page
# =========================================================================
def my_work(user):
    """Return the 6 buckets of tasks for the current user.

    A task belongs to `user` (an Employee) if either:
      - owner_user_id == user.emp_code, OR
      - owner_role    == user.role
    """
    now = datetime.utcnow()
    today_end = now.replace(hour=23, minute=59, second=59, microsecond=0)

    emp_code = getattr(user, 'emp_code', None)
    role     = getattr(user, 'role', None) or ''

    own_filter = or_(
        TaskInstance.owner_user_id == emp_code,
        and_(TaskInstance.owner_user_id.is_(None),
             TaskInstance.owner_role == role),
    )

    open_q = TaskInstance.query.filter(
        own_filter,
        TaskInstance.status.in_(TaskInstanceStatus.OPEN))

    action_required = (open_q.filter(or_(TaskInstance.due_at.is_(None),
                                         TaskInstance.due_at > today_end))
                       .order_by(TaskInstance.priority.asc(),
                                 TaskInstance.created_at.asc()).all())
    due_today = (open_q.filter(TaskInstance.due_at.isnot(None),
                               TaskInstance.due_at <= today_end,
                               TaskInstance.due_at >= now)
                 .order_by(TaskInstance.due_at.asc()).all())
    overdue = (open_q.filter(TaskInstance.due_at.isnot(None),
                             TaskInstance.due_at < now)
               .order_by(TaskInstance.due_at.asc()).all())

    my_recent_completed = (TaskInstance.query
                           .filter(TaskInstance.completed_by_id == emp_code,
                                   TaskInstance.status == TaskInstanceStatus.COMPLETED,
                                   TaskInstance.completed_at >= now - timedelta(days=14))
                           .order_by(TaskInstance.completed_at.desc())
                           .limit(50).all())

    waiting_for_others = []
    seen_entities = set()
    for mine in my_recent_completed:
        key = (mine.entity_type, mine.entity_id)
        if key in seen_entities:
            continue
        seen_entities.add(key)
        downstream = (TaskInstance.query
                      .filter(TaskInstance.entity_type == mine.entity_type,
                              TaskInstance.entity_id == mine.entity_id,
                              TaskInstance.status.in_(TaskInstanceStatus.OPEN))
                      .filter(TaskInstance.owner_user_id != emp_code)
                      .first())
        if downstream:
            waiting_for_others.append(downstream)

    recently_completed = (TaskInstance.query
                          .filter(TaskInstance.completed_by_id == emp_code,
                                  TaskInstance.status == TaskInstanceStatus.COMPLETED)
                          .order_by(TaskInstance.completed_at.desc())
                          .limit(20).all())

    exceptions = (open_q.filter(TaskInstance.status.in_(
        [TaskInstanceStatus.ESCALATED, TaskInstanceStatus.RETURNED]))
        .order_by(TaskInstance.priority.asc()).all())

    return {
        'action_required':     action_required,
        'due_today':           due_today,
        'overdue':             overdue,
        'waiting_for_others':  waiting_for_others[:20],
        'recently_completed':  recently_completed,
        'exceptions':          exceptions,
    }


def counts_for(user):
    """Fast counters for the /my-work tiles + the topbar block."""
    try:
        mw = my_work(user)
        return {
            'action_required':    len(mw['action_required']),
            'due_today':          len(mw['due_today']),
            'overdue':            len(mw['overdue']),
            'waiting_for_others': len(mw['waiting_for_others']),
            'recently_completed': len(mw['recently_completed']),
            'exceptions':         len(mw['exceptions']),
            # Convenience buckets used by the 6-KPI header on /my-work.
            'new_assignments':    len([t for t in mw['action_required']
                                       if t.age_hours < 24]),
            'rate_sourcing':      len([t for t in mw['action_required']
                                       if t.task_key == 'rfq.rate_source']),
            'quote_pending':      len([t for t in mw['action_required']
                                       if t.task_key
                                       in ('quote.prepare', 'quote.approve',
                                           'quote.submit', 'quote.followup')]),
            'followups_due':      len([t for t in mw['action_required']
                                       if t.task_key
                                       in ('account.follow_up_due',
                                           'quote.followup')]),
        }
    except Exception:
        try:
            current_app.logger.exception('task_engine.counts_for failed')
        except Exception:
            pass
        return {
            'action_required': 0, 'due_today': 0, 'overdue': 0,
            'waiting_for_others': 0, 'recently_completed': 0, 'exceptions': 0,
            'new_assignments': 0, 'rate_sourcing': 0,
            'quote_pending': 0, 'followups_due': 0,
        }
