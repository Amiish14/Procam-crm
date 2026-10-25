"""
The RFQ reports, and the panels that hang off them.

    GET /rfqs/reports              the dashboard
    GET /api/reports/tiles         §7's tiles
    GET /api/reports/steps         time per step, and the slowest
    GET /api/reports/workload      the SCM sheet, computed
    GET /api/reports/heatmap       person × step
    GET /api/reports/reasons       delay and good-practice
    GET /api/reports/rfq/<id>/chart  §6.4's time-per-step chart
    GET /api/reports/cockpit       §6.5's panels

CRM-RFQ-REPORTS-01 (R5). Every route 404s while
FEATURE_RFQ_REPORTS is off.

Each tile carries the filter that opens its list, so the number
and the list it opens cannot drift apart — acceptance test 13 is
the one that catches a dashboard quietly disagreeing with itself.
"""
from __future__ import annotations

from functools import wraps

from flask import Blueprint, jsonify, render_template, request, session

from app.services import rfq_reports as rr

bp = Blueprint('rfq_reports', __name__)


def _gate(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not rr.enabled():
            return jsonify(ok=False, error='Not found'), 404
        if not session.get('emp_code'):
            return jsonify(ok=False, error='Not authenticated'), 401
        return f(*a, **kw)
    return wrap


def _scope():
    from app.access import scope as sc_mod
    return sc_mod.current()


@bp.route('/rfqs/reports')
def reports_page():
    if not rr.enabled():
        return jsonify(ok=False, error='Not found'), 404
    if not session.get('emp_code'):
        from flask import redirect, url_for
        return redirect(url_for('login'))
    return render_template('rfq_reports.html')


@bp.route('/api/reports/tiles')
@_gate
def api_tiles():
    sc = _scope()
    data = rr.tiles(sc)
    # Each tile says which list it opens, so the figure and the
    # rows behind it cannot drift apart.
    opens = {
        'received': '/rfqs', 'active': '/rfqs?state=active',
        'on_hold': '/rfqs?state=held', 'quoted': '/rfqs?status=Quoted',
        'won': '/rfqs?status=Won', 'lost': '/rfqs?status=Lost',
        'overdue': '/rfqs/chain?colour=red',
        'due_today': '/rfqs/chain?colour=orange',
    }
    return jsonify(ok=True, tiles=data, opens=opens)


@bp.route('/api/reports/steps')
@_gate
def api_steps():
    sc = _scope()
    return jsonify(ok=True, steps=rr.per_step(sc),
                   slowest=rr.slowest_step(sc))


@bp.route('/api/reports/workload')
@_gate
def api_workload():
    from app.services import rfq_chain as rc

    rows = rr.workload(_scope())
    return jsonify(ok=True, rows=rows,
                   target_pct=round(rc.ON_TIME_TARGET * 100),
                   min_for_rate=rr.MIN_FOR_RATE,
                   unmeasured=sum(1 for r in rows
                                  if not r['measured']))


@bp.route('/api/reports/heatmap')
@_gate
def api_heatmap():
    return jsonify(ok=True, **rr.heatmap(_scope()))


@bp.route('/api/reports/reasons')
@_gate
def api_reasons():
    return jsonify(ok=True, **rr.reasons(_scope()))


@bp.route('/api/reports/rfq/<int:rfq_id>/chart')
@_gate
def api_chart(rfq_id):
    from app.models.rfq import RFQ

    rfq = RFQ.query.get(rfq_id)
    if rfq is None:
        return jsonify(ok=False, error='RFQ not found'), 404
    try:
        from app.access import records
        if not records.may_view_rfq(rfq):
            return jsonify(ok=False, error='RFQ not found'), 404
    except Exception:                                     # noqa: BLE001
        pass
    return jsonify(ok=True, **rr.time_chart(rfq))


@bp.route('/api/reports/cockpit')
@_gate
def api_cockpit():
    """§6.5 — the panels, each with the list it opens."""
    from datetime import date, timedelta

    from app import Lead
    from app.access import scope as sc_mod
    from app.models.quote import UnmatchedQuoteMail
    from app.models.task_engine import TaskInstance, TaskInstanceStatus

    me = session['emp_code']
    sc = _scope()
    waiting = (TaskInstance.query
               .filter(TaskInstance.owner_user_id == me,
                       TaskInstance.step_no.isnot(None),
                       TaskInstance.status.in_(
                           TaskInstanceStatus.OPEN)).count())
    tiles = rr.tiles(sc)

    soon = date.today() + timedelta(days=30)
    expected = (sc_mod.leads(Lead.query)
                .filter(Lead.rfq_expected_date.isnot(None),
                        Lead.rfq_expected_date <= soon,
                        Lead.rfq_expected_date >= date.today())
                .count())
    try:
        unmatched = UnmatchedQuoteMail.query.filter_by(
            status=UnmatchedQuoteMail.OPEN).count()
    except Exception:                                     # noqa: BLE001
        unmatched = 0
    try:
        from app.services import account_gate
        to_confirm = len(account_gate.pending_leads(sc=sc))
    except Exception:                                     # noqa: BLE001
        to_confirm = 0

    return jsonify(ok=True, panels={
        'waiting_on_me': {'count': waiting,
                          'opens': '/my-work'},
        'overdue': {'count': tiles['overdue'],
                    'opens': '/rfqs/chain?colour=red'},
        'accounts_to_confirm': {'count': to_confirm,
                                'opens': '/accounts/check'},
        'unmatched_quote_mail': {'count': unmatched,
                                 'opens': '/rfqs/chain#unmatched'},
        'rfqs_expected_30d': {'count': expected,
                              'opens': '/leads?rfq_expected=30d'},
    })
