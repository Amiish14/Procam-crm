"""Archive one lead, and put it back.

    GET  /api/leads/<id>/archive-state    may I, and is it archived
    POST /api/leads/<id>/archive          {reason}
    POST /api/leads/<id>/archive-restore

Not behind a feature switch. §1 is a reported absence — there is no way
to archive a lead from the screen at all — and a fix for an absence
that is itself switched off leaves the absence in place.

A refusal answers 403 with the sentence `lead_archive` wrote, so the
screen can show the reason instead of a status code. A bad reason
answers 400 for the same reason: the person needs to know it was the
reason that was rejected and not their permission.
"""
from __future__ import annotations

from flask import Blueprint, abort, jsonify, request, session

bp = Blueprint('lead_archive', __name__)


def _me():
    return session.get('emp_code')


def _lead(lid):
    from app import Lead

    lead = Lead.query.get(lid)
    if lead is None:
        abort(404)
    return lead


@bp.route('/api/leads/<int:lid>/archive-state')
def state(lid):
    from app.services import lead_archive as la

    code = _me()
    if not code:
        return jsonify(ok=False, error='Sign in first.'), 401
    return jsonify(ok=True, **la.state_for(_lead(lid), code))


@bp.route('/api/leads/<int:lid>/archive', methods=['POST'])
def archive(lid):
    from app.services import lead_archive as la

    code = _me()
    if not code:
        return jsonify(ok=False, error='Sign in first.'), 401
    lead = _lead(lid)
    body = request.get_json(silent=True) or {}
    try:
        out = la.archive(lead, reason=body.get('reason'), actor=code)
    except la.Refused as refusal:
        # 403 when they may not, 400 when the reason was the problem.
        # The screen shows the same sentence either way; the code is
        # what a log reader needs to tell the two apart.
        allowed = la.may_archive(lead, code)
        return jsonify(ok=False, error=str(refusal)), (400 if allowed else 403)
    # `archived` in the state is the flag; the count goes under its
    # own name so the two cannot collide.
    return jsonify(ok=True, archived_count=out.get('archived', 0),
                   batch_ref=out.get('batch_ref', ''),
                   **la.state_for(lead, code))


@bp.route('/api/leads/<int:lid>/archive-restore', methods=['POST'])
def restore(lid):
    from app.services import lead_archive as la

    code = _me()
    if not code:
        return jsonify(ok=False, error='Sign in first.'), 401
    lead = _lead(lid)
    try:
        out = la.restore(lead, actor=code)
    except la.Refused as refusal:
        allowed = la.may_archive(lead, code)
        return jsonify(ok=False, error=str(refusal)), (400 if allowed else 403)
    return jsonify(ok=True, restored_count=out.get('restored', 0),
                   **la.state_for(lead, code))
