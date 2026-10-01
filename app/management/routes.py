"""
Management Command View — /management.

Every figure on this page links to the records behind it, inside the
reader's own Access Matrix boundary. A management screen whose numbers
cannot be opened is a scoreboard; the numbers only change when somebody
can get from the tile to the record and act on it.

The arithmetic lives in `app/review/service.py::command_view` so the
command view, the individual review and the vertical review cannot
disagree about what "late", "stuck" or "inactive" means.
"""
from __future__ import annotations

from functools import wraps

from flask import (Blueprint, jsonify, redirect, render_template, request,
                   session)

from app.access import scope as sc_mod
from app.review import service as rv
from app.services import sales_rules as rules
from app.services.urls import login_url as _login_url

bp = Blueprint('management', __name__)


def _signed_in(f):
    """A session, then the Access Matrix per record: the command view is
    the viewer's own boundary, so a desk head sees their desk and a
    director sees the company."""
    @wraps(f)
    def wrap(*a, **kw):
        if not session.get('emp_code'):
            if request.path.startswith('/api/'):
                return jsonify(ok=False, error='Not authenticated'), 401
            return redirect(_login_url())
        return f(*a, **kw)
    return wrap


def _manages():
    """The command view is for someone who looks after other people's
    work. A viewer who can only see their own records has the Daily
    Workbench for exactly that, and a page of their own figures labelled
    "management" misleads them about what they are looking at."""
    sc = sc_mod.current()
    if sc.unrestricted or sc.can('admin.access'):
        return sc
    if (sc.codes or set()) - {sc.emp_code}:
        return sc
    return None


@bp.route('/management')
@_signed_in
def home():
    sc = _manages()
    if sc is None:
        return render_template('access_denied.html',
                               need='a team to look after'), 403
    data = rv.command_view(sc=sc)
    return render_template('management/home.html', cv=data, rules=rules,
                           scope=('company' if sc.unrestricted
                                  else sc.data_scope))


@bp.route('/api/management')
@_signed_in
def api_home():
    sc = _manages()
    if sc is None:
        return jsonify(ok=False,
                       error='The command view is for managers; your own '
                             'work is on the Daily Workbench.'), 403
    return jsonify(ok=True, **rv.command_view(sc=sc))
