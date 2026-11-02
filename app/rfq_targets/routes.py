"""
Admin → RFQ targets: the one screen Module 3.2 asks for.

    GET   /admin/rfq-targets                 the screen
    GET   /api/rfq-targets                   this month's targets
    GET   /api/rfq-targets/proposals         what the seed would write
    POST  /api/rfq-targets                   set or change one
    GET   /api/rfq-targets/history           every change

Module 3.2: "This is the starting point. Admin can edit any target
later on one screen, with history kept."

Setting a target is administrators only. A target is what somebody is
reviewed against, so a salesperson editing their own would be the one
permission in this CRM that nobody would admit to wanting. Reading is
wide: a person who cannot see their target cannot work towards it.
"""
from __future__ import annotations

from functools import wraps

from flask import (Blueprint, jsonify, render_template, request,
                   session)

from app import db
from app.services import rfq_targets as rt

bp = Blueprint('rfq_targets', __name__)


def _me():
    return session.get('emp_code')


def _auth(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not _me():
            from app.services import guided_help
            body, status = guided_help.refusal('SESSION_EXPIRED',
                                               http=401)
            return jsonify(body), status
        return f(*a, **kw)
    return wrap


def _admin_only(f):
    @wraps(f)
    def wrap(*a, **kw):
        from app import Employee

        if not _me():
            from flask import redirect, url_for
            return redirect(url_for('login'))
        emp = Employee.query.filter_by(emp_code=_me()).first()
        role = (getattr(emp, 'role', '') or '').lower()
        if role not in ('admin', 'super admin', 'superadmin',
                        'director', 'procam_admin'):
            return jsonify(
                ok=False,
                error='Targets are set by an administrator. A target '
                      'is what you are reviewed against.'), 403
        return f(*a, **kw)
    return wrap


def _rows_for(period):
    from app import Employee

    out = []
    for row in rt.for_period(period):
        data = row.to_dict()
        emp = Employee.query.filter_by(emp_code=row.emp_code).first()
        data['name'] = getattr(emp, 'name', '') or row.emp_code
        data['active'] = bool(getattr(emp, 'is_active', False))
        out.append(data)
    return out


@bp.route('/admin/rfq-targets')
@_admin_only
def page():
    period = rt.month_start()
    proposals = rt.proposals()
    return render_template(
        'admin/rfq_targets.html',
        period=period, rows=_rows_for(period),
        proposals=proposals,
        history=[h.to_dict() for h in rt.history(limit=50)])


@bp.route('/api/rfq-targets')
@_auth
def api_list():
    period = rt.month_start()
    return jsonify(ok=True, period=str(period),
                   rows=_rows_for(period))


@bp.route('/api/rfq-targets/proposals')
@_admin_only
def api_proposals():
    found = rt.proposals()
    return jsonify(ok=True, period=str(found['period']),
                   window=[str(found['from']), str(found['to'])],
                   source=found['source'], rows=found['rows'],
                   no_history=found['no_history'])


@bp.route('/api/rfq-targets', methods=['POST'])
@_admin_only
def api_set():
    data = request.get_json(silent=True) or {}
    try:
        row = rt.set_target(
            data.get('emp_code'), data.get('vertical'),
            data.get('target'), actor=_me(),
            reason=(data.get('reason') or ''), source='manual')
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc),
                       needs='target'), 400
    db.session.commit()
    return jsonify(ok=True, target=row.to_dict())


@bp.route('/api/rfq-targets/history')
@_auth
def api_history():
    rows = rt.history(emp_code=(request.args.get('emp_code') or None),
                      vertical=(request.args.get('vertical') or None))
    return jsonify(ok=True, history=[r.to_dict() for r in rows])
