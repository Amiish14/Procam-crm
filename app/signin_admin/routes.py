"""Why can this person not sign in. Read-only.

CRM-WA-01 §5.

    GET /api/admin/signin-check?code=XXX

`/login` answers the same sentence for every failure on purpose:
telling an anonymous caller "that code exists but the password is
wrong" tells them which codes exist. That does not change. This is
the other side of the desk — an administrator, already signed in,
asking why a named colleague cannot get in.

Admin only, through the Access Matrix (`admin.employees`), not a role
name: the matrix is what every other employee-administration screen
asks, and a second answer computed here would disagree with it
somewhere.

It never reads, returns or logs a password or a hash, and it never
says whether a given password would work. There is no POST: the
reset that fixes most of these already exists at
`PUT /api/employees/<id>` with `{"reset_password": true}`, behind the
Reset password button on Admin → Employees, and a second way to
issue a credential is a second thing to get wrong.
"""
from __future__ import annotations

from functools import wraps

from flask import Blueprint, jsonify, request, session

bp = Blueprint('signin_admin', __name__)


def _signin_admin(f):
    """Signed in, and allowed to administer people.

    Its own decorator so the route audit in
    scripts/generate_reference_docs.py names the gate rather than
    reporting this as open.
    """
    @wraps(f)
    def wrap(*a, **kw):
        if not session.get('emp_code'):
            return jsonify(ok=False, error='Sign in first.'), 401
        try:
            from app.access.service import can
            allowed = bool(can('admin.employees'))
        except Exception:                                 # noqa: BLE001
            allowed = False
        if not allowed:
            return jsonify(
                ok=False,
                error='Only an administrator can look up why somebody '
                      'cannot sign in.'), 403
        return f(*a, **kw)
    return wrap


@bp.route('/api/admin/signin-check')
@_signin_admin
def signin_check():
    from app.services import signin_diagnosis as sd

    code = request.args.get('code') or ''
    out = sd.diagnose(code)
    out['failures'] = sd.recent_failures(out['code'])
    # Said in the response rather than only in the docstring: the
    # caller is a screen, and the screen should be able to tell a
    # person that the reset button is the next step.
    out['reset_is'] = ('Admin → Employees → Reset password, which also '
                       'clears a lock and signs out the old sessions.')
    return jsonify(ok=True, **out)
