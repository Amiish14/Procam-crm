"""
Admin Lead Triage — WP4.

    GET  /lead-triage                 the dashboard
    GET  /api/triage/summary          headline + buckets + both tables
    POST /api/triage/assign           assign one lead from the dashboard

Admin-only, enforced on the server. The permission is a real entry in the
access matrix (admin.triage) rather than a role check in the template, so
it is administered the same way as everything else and cannot be reached
by calling the API directly.
"""
from flask import Blueprint, jsonify, render_template, request, session

from app import db
from app.access.service import require
from app.triage import service as triage


bp = Blueprint('triage', __name__)

_PERM = 'admin.triage'


def _sla():
    try:
        v = int(request.args.get('sla') or triage.DEFAULT_SLA_HOURS)
    except (TypeError, ValueError):
        return triage.DEFAULT_SLA_HOURS
    return max(1, min(v, 720))


@bp.route('/lead-triage')
@require(_PERM)
def page():
    return render_template('triage/dashboard.html',
                           sla_hours=triage.DEFAULT_SLA_HOURS)


@bp.route('/api/triage/summary', methods=['GET'])
@require(_PERM)
def api_summary():
    return jsonify(ok=True, **triage.summary(sla_hours=_sla()))


@bp.route('/api/triage/assign', methods=['POST'])
@require(_PERM)
def api_assign():
    """Assign from the dashboard, through the WP5 path.

    Not a second implementation: the same service the lead form and bulk
    assign use, so the history row and both notifications happen here too.
    """
    from app import Lead
    from app.services import lead_assignment

    d = request.get_json(silent=True) or {}
    lead_id = d.get('lead_id')
    lead = db.session.get(Lead, int(lead_id)) if lead_id else None
    if lead is None:
        return jsonify(ok=False, error='Lead not found'), 404

    ok, err = lead_assignment.assign(
        lead,
        primary_code=d.get('primary') if 'primary' in d else None,
        secondary_code=d.get('secondary') if 'secondary' in d else None,
        actor=session.get('emp_code'), note='lead triage')
    if not ok:
        db.session.rollback()
        return jsonify(ok=False, error=err), 400

    db.session.commit()
    # The caller updates its counts from this rather than reloading.
    return jsonify(ok=True, lead_id=lead.id,
                   headline=triage.headline(),
                   buckets=triage.buckets())
