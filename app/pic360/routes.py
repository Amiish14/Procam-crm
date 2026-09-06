"""PIC 360 — §22."""
from flask import (Blueprint, jsonify, render_template, request, session)

from app.pic360 import service as p360

bp = Blueprint('pic360', __name__)


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


@bp.route('/people')
@_login_required
def people_list():
    """Everyone the viewer is authorised to see, with their workload."""
    from app import Employee

    allowed = p360.visible_codes()
    q = Employee.query.filter(Employee.is_active.is_(True))
    if allowed is not None:
        q = q.filter(Employee.emp_code.in_(allowed or ['']))

    term = (request.args.get('q') or '').strip()
    if term:
        q = q.filter(Employee.name.ilike(f'%{term}%'))

    rows = q.order_by(Employee.name).all()
    loads = {e.emp_code: p360.workload(e.emp_code) for e in rows}
    return render_template('pic/list.html', people=rows, loads=loads,
                           term=term, scoped=(allowed is not None))


@bp.route('/people/<emp_code>')
@_login_required
def pic_360(emp_code):
    from app import Employee

    employee = Employee.query.filter_by(emp_code=emp_code).first()
    if employee is None:
        return render_template('pic/not_found.html', emp_code=emp_code), 404

    # §22 says "authorised" — a vertical head sees their own vertical, not
    # every colleague. Refusing here rather than filtering the data keeps
    # this consistent with the reports.
    if not p360.may_view(emp_code):
        return render_template('access_denied.html',
                               need='visibility of this person'), 403

    return render_template('pic/detail.html', employee=employee,
                           data=p360.full(employee))


@bp.route('/api/people/<emp_code>/360')
@_login_required
def api_pic_360(emp_code):
    from app import Employee

    employee = Employee.query.filter_by(emp_code=emp_code).first()
    if employee is None:
        return jsonify(ok=False, error='No such employee'), 404
    if not p360.may_view(emp_code):
        return jsonify(ok=False, error='Not authorised to view this '
                                       'person'), 403
    return jsonify(ok=True, **p360.full(employee))
