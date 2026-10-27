"""
My Work — role-aware landing page (Phase 4 of the CRM upgrade).

Endpoints:
    GET /my-work        HTML landing page (hero + 6 KPIs + 6 buckets).
    GET /my-work.json   Same data as JSON — used by the SPA and by the
                        Pending Actions block injected on every dashboard
                        page.
"""
from flask import Blueprint, render_template, jsonify, session, redirect, url_for

from app import db  # noqa: F401 — ensures models are registered
from app.services.task_engine import my_work, counts_for


bp = Blueprint('my_work', __name__)


def _current_emp():
    """Resolve the current Employee from the session. Returns None if
    the user is not authenticated."""
    emp_code = session.get('emp_code')
    if not emp_code:
        return None
    try:
        from app import Employee
        return Employee.query.filter_by(emp_code=emp_code).first()
    except Exception:
        return None


def _serialize_bucket(rows):
    out = []
    for r in rows or []:
        try:
            out.append(r.to_dict())
        except Exception:
            pass
    return out


def _chain_steps(emp):
    """The RFQ steps waiting on this person (§4.8).

    Empty list when FEATURE_RFQ_CHAIN is off for them, so the page
    is exactly what it was.
    """
    try:
        from app.services import rfq_chain
        return rfq_chain.waiting_on(getattr(emp, 'emp_code', None))
    except Exception:                                     # noqa: BLE001
        return []


@bp.route('/my-work/tasks')
def my_work_home():
    emp = _current_emp()
    if not emp:
        return redirect(url_for('login'))
    counts = counts_for(emp)
    buckets = my_work(emp)
    return render_template('my_work/home.html',
                           emp=emp,
                           counts=counts,
                           chain_steps=_chain_steps(emp),
                           action_required=buckets['action_required'],
                           due_today=buckets['due_today'],
                           overdue=buckets['overdue'],
                           waiting_for_others=buckets['waiting_for_others'],
                           recently_completed=buckets['recently_completed'],
                           exceptions=buckets['exceptions'])


@bp.route('/my-work.json')
def my_work_json():
    emp = _current_emp()
    if not emp:
        return jsonify(ok=False, error='login required'), 401
    counts = counts_for(emp)
    buckets = my_work(emp)
    return jsonify(
        ok=True,
        emp={'emp_code': emp.emp_code, 'name': emp.name, 'role': emp.role},
        counts=counts,
        chain_steps=_chain_steps(emp),
        buckets={
            'action_required':    _serialize_bucket(buckets['action_required']),
            'due_today':          _serialize_bucket(buckets['due_today']),
            'overdue':            _serialize_bucket(buckets['overdue']),
            'waiting_for_others': _serialize_bucket(buckets['waiting_for_others']),
            'recently_completed': _serialize_bucket(buckets['recently_completed']),
            'exceptions':         _serialize_bucket(buckets['exceptions']),
        },
    )
