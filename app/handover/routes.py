"""
Won → TMS Handover routes (Phase 8 of the CRM upgrade).

Endpoints:
    HTML:
        GET  /handovers                  queue view (TMS-side users)

    JSON:
        POST  /api/handovers             create (auto by state hook, or manual)
        GET   /api/handovers             queue list, filterable
        GET   /api/handovers/<id>        detail
        PATCH /api/handovers/<id>        TMS admin fills tms_project_id etc.
        POST  /api/handovers/<id>/cancel

State machine (managed here):
    Handover Pending → TMS Project Created → Handover Complete
                     ↘ Cancelled
"""
from datetime import datetime
from functools import wraps

from flask import (Blueprint, jsonify, render_template, request, session,
                   redirect, url_for, current_app)

from app import db
from app.models.tms_handover import WonHandover
from app.services.task_engine import on_state_change
from app.models.task_engine import TaskInstance, TaskInstanceStatus


bp = Blueprint('handover', __name__)


def _require_auth(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not session.get('emp_code'):
            return jsonify(error='Not authenticated'), 401
        return f(*a, **kw)
    return wrap


def _current_emp():
    ec = session.get('emp_code')
    if not ec:
        return None
    try:
        from app import Employee
        return Employee.query.filter_by(emp_code=ec).first()
    except Exception:
        return None


def _fire(entity, entity_type, old_state, new_state):
    try:
        emp = _current_emp()
        on_state_change(entity, entity_type, old_state, new_state,
                        triggered_by=emp)
    except Exception:
        try:
            current_app.logger.exception(
                'handover.on_state_change failed for %s#%s %s→%s',
                entity_type, getattr(entity, 'id', '?'),
                old_state, new_state)
        except Exception:
            pass


_STATUSES = ['Handover Pending', 'TMS Project Created',
             'Handover Complete', 'Cancelled']


# ─── HTML ──────────────────────────────────────────────────────────────────
@bp.route('/handovers')
def queue_page():
    if not session.get('emp_code'):
        return redirect(url_for('login'))
    return render_template('handover/queue.html', emp=_current_emp())


# ─── JSON API ──────────────────────────────────────────────────────────────
@bp.route('/api/handovers', methods=['GET'])
@_require_auth
def api_list():
    q = WonHandover.query
    status = request.args.get('status') or 'Handover Pending'
    if status and status != 'all':
        q = q.filter(WonHandover.status == status)
    rows = q.order_by(WonHandover.created_at.desc()).limit(500).all()
    return jsonify(ok=True, handovers=[r.to_dict() for r in rows])


@bp.route('/api/handovers', methods=['POST'])
@_require_auth
def api_create():
    d = request.get_json(silent=True) or {}
    actor = session.get('emp_code')
    row = WonHandover(
        opportunity_id=d.get('opportunity_id') or None,
        quote_id=d.get('quote_id') or None,
        rfq_id=d.get('rfq_id') or None,
        account_id=d.get('account_id') or None,
        project_id=d.get('project_id') or None,
        account_name=(d.get('account_name') or '').strip() or None,
        won_value=d.get('won_value') or 0,
        services=list(d.get('services') or []),
        origin=(d.get('origin') or '').strip() or None,
        destination=(d.get('destination') or '').strip() or None,
        scope=(d.get('scope') or '').strip() or None,
        vertical=(d.get('vertical') or '').strip() or None,
        pic_emp_code=(d.get('pic_emp_code') or '').strip() or None,
        po_ref=(d.get('po_ref') or '').strip() or None,
        attachments=list(d.get('attachments') or []),
        commercial_refs=dict(d.get('commercial_refs') or {}),
        status='Handover Pending',
        created_by_id=actor,
    )

    # Dedup by opp_id if given
    if row.opportunity_id:
        existing = WonHandover.query.filter_by(
            opportunity_id=row.opportunity_id).first()
        if existing:
            return jsonify(ok=True, handover=existing.to_dict(), noop=True)

    db.session.add(row)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error='commit failed'), 500
    _fire(row, 'WonHandover', None, 'Handover Pending')
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return jsonify(ok=True, handover=row.to_dict())


@bp.route('/api/handovers/<int:hid>', methods=['GET'])
@_require_auth
def api_get(hid):
    row = WonHandover.query.get_or_404(hid)
    return jsonify(ok=True, handover=row.to_dict())


@bp.route('/api/handovers/<int:hid>', methods=['PATCH'])
@_require_auth
def api_patch(hid):
    row = WonHandover.query.get_or_404(hid)
    if row.status == 'Cancelled':
        return jsonify(ok=False, error='Handover is Cancelled — immutable'), 400
    d = request.get_json(silent=True) or {}
    old_status = row.status

    for f in ('tms_project_id', 'tms_job_id', 'tms_ack_by', 'remarks',
              'account_name', 'origin', 'destination', 'scope', 'vertical',
              'pic_emp_code', 'po_ref'):
        if f in d:
            setattr(row, f, (d.get(f) or None))
    if 'services' in d:
        row.services = list(d.get('services') or [])
    if 'attachments' in d:
        row.attachments = list(d.get('attachments') or [])
    if 'commercial_refs' in d:
        row.commercial_refs = dict(d.get('commercial_refs') or {})
    if 'won_value' in d:
        try:
            row.won_value = float(d.get('won_value') or 0)
        except Exception:
            pass
    if d.get('tms_ack_by'):
        row.tms_ack_at = datetime.utcnow()
    if d.get('tms_project_id') and not row.tms_created_at:
        row.tms_created_at = datetime.utcnow()

    # Direct status write allowed (admin flow)
    if 'status' in d and d.get('status') in _STATUSES:
        row.status = d.get('status')
    else:
        # Auto-advance when both project_id + ack set
        if row.tms_project_id and row.tms_ack_at \
           and row.status == 'Handover Pending':
            row.status = 'TMS Project Created'
        # 'complete' keyword in remarks marks Complete
        rem = (d.get('remarks') or row.remarks or '').lower()
        if 'complete' in rem and row.status == 'TMS Project Created':
            row.status = 'Handover Complete'

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error='commit failed'), 500

    if row.status != old_status:
        _fire(row, 'WonHandover', old_status, row.status)
        # Auto-close open deal.won.handoff tasks when handover completes
        if row.status in ('TMS Project Created', 'Handover Complete') \
           and row.opportunity_id:
            try:
                open_tasks = (TaskInstance.query
                              .filter(TaskInstance.task_key == 'deal.won.handoff',
                                      TaskInstance.entity_type == 'Opportunity',
                                      TaskInstance.entity_id == row.opportunity_id,
                                      TaskInstance.status.in_(TaskInstanceStatus.OPEN))
                              .all())
                for t in open_tasks:
                    t.status = TaskInstanceStatus.COMPLETED
                    t.completed_at = datetime.utcnow()
                    t.completed_by_id = session.get('emp_code')
            except Exception:
                pass
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

    return jsonify(ok=True, handover=row.to_dict())


@bp.route('/api/handovers/<int:hid>/cancel', methods=['POST'])
@_require_auth
def api_cancel(hid):
    row = WonHandover.query.get_or_404(hid)
    if row.status in ('Handover Complete', 'Cancelled'):
        return jsonify(ok=False, error=f'Handover is {row.status}'), 400
    d = request.get_json(silent=True) or {}
    reason = (d.get('reason') or '').strip()
    old = row.status
    row.status = 'Cancelled'
    if reason:
        row.remarks = ((row.remarks or '') +
                       f'\n[Cancelled {datetime.utcnow():%Y-%m-%d %H:%M}] {reason}').strip()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error='commit failed'), 500
    _fire(row, 'WonHandover', old, 'Cancelled')
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return jsonify(ok=True, handover=row.to_dict())
