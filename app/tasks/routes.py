"""
Finishing a task.

    GET  /api/tasks?lead=<id>      the open tasks on a record
    POST /api/tasks/<id>/done      mark one done, with an optional note

Until 19 October there was no way to finish a task at all. `/my-work/tasks`
and `/my-work.json` were both GET, and tasks closed only as a side
effect of an entity reaching a state some TaskDefinition named as its
completion. That is the first of the five reasons nothing had closed
since 17 August: not that closing was broken, but that nobody could
ask for it.

Who may: the person who owns it, anyone whose data scope reaches that
owner, and anyone holding `admin.triage`. A role-owned task — one with
no named person, escalated to Vertical_Head — may be closed by anyone
whose scope is wider than their own leads, because otherwise it
belongs to everybody and therefore to nobody.

Completed, not Cancelled: a person pressing Done did the work. The
bulk scripts cancel, because nobody did those.
"""
from __future__ import annotations

from datetime import datetime
from functools import wraps

from flask import Blueprint, jsonify, request, session

from app import db
from app.models.task_engine import TaskInstance, TaskInstanceStatus

bp = Blueprint('tasks', __name__)

#: A note on a finished task is a sentence, not a report.
MAX_NOTE = 1000


def _signed_in(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not session.get('emp_code'):
            return jsonify(ok=False, error='Not authenticated'), 401
        return f(*a, **kw)
    return wrap


def may_close(task, sc, emp_code, can_triage=False):
    """Whether this viewer may finish this task.

    Split out and named so the rule can be tested directly rather
    than inferred from HTTP status codes.
    """
    if can_triage or sc.unrestricted:
        return True
    owner = (task.owner_user_id or '').strip()
    if owner:
        return owner == emp_code or sc.reaches(owner)
    # Role-owned: no named person. Anybody with more than their own
    # leads in scope may clear it — otherwise an escalation to
    # Vertical_Head can be closed by nobody.
    return bool(sc.codes is None or len(sc.codes or ()) > 1)


@bp.route('/api/tasks')
@_signed_in
def api_tasks():
    """The tasks on one record, open first, newest first within that."""
    from app.access import scope as sc_mod

    entity_type = (request.args.get('entity_type') or 'Lead').strip()
    raw = request.args.get('lead') or request.args.get('entity_id')
    try:
        entity_id = int(raw)
    except (TypeError, ValueError):
        return jsonify(ok=False, error='Which record?'), 400

    if entity_type == 'Lead':
        from app import Lead
        lead = sc_mod.leads().filter(Lead.id == entity_id).first()
        if lead is None:
            return jsonify(ok=False, error='Not found'), 404

    sc = sc_mod.current()
    me = session.get('emp_code')
    can_triage = _can_triage()
    rows = (TaskInstance.query
            .filter(TaskInstance.entity_type == entity_type,
                    TaskInstance.entity_id == entity_id)
            .order_by(TaskInstance.id.desc()).all())
    out = []
    for r in rows:
        d = r.to_dict()
        d['is_open'] = r.is_open
        d['may_close'] = bool(r.is_open
                              and may_close(r, sc, me, can_triage))
        out.append(d)
    out.sort(key=lambda d: (not d['is_open'], -d['id']))
    return jsonify(ok=True, tasks=out,
                   open_count=sum(1 for d in out if d['is_open']))


def _can_triage():
    try:
        from app import _matrix_can
        return bool(_matrix_can('admin.triage'))
    except Exception:                                     # noqa: BLE001
        return False


@bp.route('/api/tasks/<int:tid>/done', methods=['POST'])
@_signed_in
def api_task_done(tid):
    """Mark one task finished. The first way there has ever been."""
    from app.access import scope as sc_mod
    from app.services import audit

    task = db.session.get(TaskInstance, tid)
    if task is None:
        return jsonify(ok=False, error='No such task'), 404

    me = session.get('emp_code')
    if not may_close(task, sc_mod.current(), me, _can_triage()):
        return jsonify(ok=False,
                       error='That task is outside your access'), 403

    if not task.is_open:
        # Not an error: two people pressing Done on the same row is
        # ordinary, and the second should see the same outcome as the
        # first rather than a failure.
        return jsonify(ok=True, already=True, task=task.to_dict())

    body = request.get_json(silent=True) or {}
    note = (body.get('note') or '').strip()
    if len(note) > MAX_NOTE:
        return jsonify(ok=False,
                       error=f'Keep the note under {MAX_NOTE} characters'), 400

    was = task.status
    task.status = TaskInstanceStatus.COMPLETED
    task.completed_at = datetime.utcnow()
    task.completed_by_id = me
    if note:
        task.notes = ((task.notes + '\n') if task.notes else '') + note
    audit.record('task.completed', 'task_instance', str(task.id),
                 old={'status': was},
                 new={'status': TaskInstanceStatus.COMPLETED,
                      'by': me, 'note': note},
                 actor=me, reason=(note or 'Marked done')[:200])
    db.session.commit()

    # The board caches hygiene for a minute; a task the user has just
    # finished should not still be on their screen after a refresh.
    try:
        from app.workbench import service as wb
        wb.hygiene_cache_clear()
    except Exception:                                     # noqa: BLE001
        pass
    return jsonify(ok=True, task=task.to_dict())
