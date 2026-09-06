"""Training Academy — §73-81."""
from flask import (Blueprint, jsonify, render_template, request, session,
                   redirect)

from app.access.service import require
from app.training import service as tr
from app.training.content import LEVELS, BY_KEY, CONTENT_VERSION

bp = Blueprint('training', __name__)


def _login_required(f):
    from functools import wraps

    @wraps(f)
    def wrap(*a, **kw):
        if not session.get('emp_code'):
            if request.path.startswith('/api/'):
                return jsonify(ok=False, error='Not authenticated'), 401
            return ('', 302, {'Location': '/login'})
        return f(*a, **kw)
    return wrap


@bp.route('/academy')
@_login_required
def academy():
    me = session.get('emp_code')
    return render_template('training/index.html',
                           levels=tr.progress_for(me),
                           summary=tr.summary_for(me))


@bp.route('/academy/<level_key>')
@_login_required
def level(level_key):
    spec = BY_KEY.get(level_key)
    if spec is None:
        return redirect('/academy')
    me = session.get('emp_code')
    state = {p['key']: p for p in tr.progress_for(me)}[level_key]
    return render_template('training/level.html', spec=spec, state=state,
                           version=CONTENT_VERSION)


@bp.route('/api/academy/<level_key>/learned', methods=['POST'])
@_login_required
def api_learned(level_key):
    try:
        tr.mark_learned(session.get('emp_code'), level_key)
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 404
    return jsonify(ok=True)


@bp.route('/api/academy/<level_key>/practice', methods=['POST'])
@_login_required
def api_practice(level_key):
    d = request.get_json(silent=True) or {}
    try:
        correct, feedback = tr.check_practice(session.get('emp_code'),
                                              level_key, d.get('answer'))
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 404
    return jsonify(ok=True, correct=correct, feedback=feedback)


@bp.route('/api/academy/<level_key>/quiz', methods=['POST'])
@_login_required
def api_quiz(level_key):
    d = request.get_json(silent=True) or {}
    try:
        result = tr.check_quiz(session.get('emp_code'), level_key,
                               d.get('answers') or [])
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 404
    return jsonify(ok=True, **result)


@bp.route('/academy/certificate')
@_login_required
def certificate():
    """§79 — printable, with a verifiable id."""
    from app import Employee
    me = session.get('emp_code')
    emp = Employee.query.filter_by(emp_code=me).first()
    cert, problem = tr.issue_certificate(me, emp.name if emp else me)
    if cert is None:
        return render_template('training/not_yet.html', problem=problem,
                               summary=tr.summary_for(me)), 200
    return render_template('training/certificate.html', cert=cert,
                           employee=emp)


@bp.route('/admin/training')
@require('admin.master')
def management():
    """§80 — completion across the team."""
    return render_template('training/management.html', **tr.management_view())
