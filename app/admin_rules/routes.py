"""
Admin → CRM rules: what the pilot still needs.

    GET /admin/crm-rules       the panel
    GET /api/admin/crm-rules   the same thing as JSON

The spec points at `/admin/crm-rules` in four places and the page
had never been built, so every setting it describes had to be set
from a shell. This is that page — read-only for now, because the
settings behind it are written by scripts that validate names,
and a text box that writes an unchecked employee code into
`rfq_coordinator` would undo that.
"""
from __future__ import annotations

from functools import wraps

from flask import Blueprint, jsonify, render_template, session

bp = Blueprint('admin_rules', __name__)


def _admin_only(f):
    @wraps(f)
    def wrap(*a, **kw):
        from app import Employee

        code = session.get('emp_code')
        if not code:
            from flask import redirect, url_for
            return redirect(url_for('login'))
        emp = Employee.query.filter_by(emp_code=code).first()
        role = (getattr(emp, 'role', '') or '').lower()
        if role not in ('admin', 'super admin', 'superadmin',
                        'director'):
            return jsonify(ok=False,
                           error='Admins and directors only.'), 403
        return f(*a, **kw)
    return wrap


@bp.route('/admin/crm-rules')
@_admin_only
def page():
    from app.services import pilot_readiness as pr
    return render_template('admin/crm_rules.html', data=pr.report())


@bp.route('/api/admin/crm-rules')
@_admin_only
def api():
    from app.services import pilot_readiness as pr
    return jsonify(ok=True, **pr.report())
