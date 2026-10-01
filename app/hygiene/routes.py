"""
The CRM Hygiene Score — the page and its API.

    /hygiene                    the score, factor by factor
    /hygiene/factor/<key>       the records behind one factor
    /api/hygiene                the same score as JSON
    /api/hygiene/factor/<key>   the same records as JSON
    /api/hygiene/team           a score per person the viewer reaches
    /api/hygiene/nudge          tell a colleague their records need work

Visibility is the Access Matrix's, through `app.access.scope`, and the
arithmetic is `app.services.hygiene`. `?for=` and `?vertical=` narrow
within the viewer's boundary; neither can widen it, and asking about
somebody outside it is refused rather than answered with nothing.
"""
from __future__ import annotations

from functools import wraps

from flask import (Blueprint, jsonify, redirect, render_template, request,
                   session, url_for)

from app.access import scope as sc_mod
from app.services import hygiene

bp = Blueprint('hygiene', __name__)


def _need_login():
    return jsonify(ok=False, error='Not authenticated'), 401


def _signed_in(f):
    """Everything here needs a session; what it then shows is decided by
    the Access Matrix scope, per record."""
    @wraps(f)
    def wrap(*a, **kw):
        if not session.get('emp_code'):
            if request.path.startswith('/api/'):
                return _need_login()
            return redirect(url_for('login'))
        return f(*a, **kw)
    return wrap


def _args():
    """The narrowing the caller asked for, as given."""
    return ((request.args.get('for') or '').strip().upper(),
            (request.args.get('vertical') or '').strip())


def _refused(exc):
    return jsonify(ok=False, error=str(exc)), 403


@bp.route('/api/hygiene')
@_signed_in
def api_score():
    sc = sc_mod.current()
    who, vertical = _args()
    try:
        data = hygiene.score(sc, emp_code=who or None,
                             vertical=vertical or None)
    except hygiene.OutsideScope as exc:
        return _refused(exc)
    data.update(ok=True, viewer=session.get('emp_code'),
                levels=hygiene.levels(sc),
                verticals=hygiene.verticals_for(sc))
    return jsonify(data)


@bp.route('/api/hygiene/factor/<key>')
@_signed_in
def api_factor(key):
    sc = sc_mod.current()
    who, vertical = _args()
    try:
        page = max(1, int(request.args.get('page') or 1))
    except ValueError:
        page = 1
    try:
        data = hygiene.factor_records(sc, key, emp_code=who or None,
                                      vertical=vertical or None, page=page)
    except hygiene.OutsideScope as exc:
        return _refused(exc)
    if data is None:
        return jsonify(ok=False, error=f'Unknown factor "{key}"'), 404
    return jsonify(ok=True, **data)


@bp.route('/api/hygiene/team')
@_signed_in
def api_team():
    """A score each for the people the viewer's scope reaches.

    A viewer whose scope is only themselves gets an empty list, not a
    refusal: there is no team to show, which is not an error.
    """
    sc = sc_mod.current()
    return jsonify(ok=True, people=hygiene.team(sc))


@bp.route('/api/hygiene/nudge', methods=['POST'])
@_signed_in
def api_nudge():
    """Ask a colleague to bring their records up to date.

    Goes through the one notification path, so it reaches the bell, the
    inbox and the audit trail like everything else, and cannot be sent
    to somebody the sender may not see.
    """
    from app import Employee
    from app.services import notify

    body = request.get_json(silent=True) or {}
    to = (body.get('to') or '').strip().upper()
    note = (body.get('message') or '').strip()
    factor = (body.get('factor') or '').strip()
    sc = sc_mod.current()
    if not to:
        return jsonify(ok=False, error='Say who this is for'), 400
    if to == (session.get('emp_code') or '').upper():
        return jsonify(ok=False, error='That is you'), 400
    if not sc.unrestricted and not sc.reaches(to):
        return jsonify(ok=False,
                       error='That person is outside your access'), 403
    if len(note) > 500:
        return jsonify(ok=False,
                       error='Keep the note under 500 characters'), 400
    emp = Employee.query.filter_by(emp_code=to, is_active=True).first()
    if emp is None:
        return jsonify(ok=False, error='No such active employee'), 404
    where = '/hygiene' + (f'/factor/{factor}' if factor in hygiene.BY_KEY
                          else '')
    label = (hygiene.BY_KEY[factor]['label'] if factor in hygiene.BY_KEY
             else 'CRM hygiene')
    sent = notify.send(
        to, kind='hygiene_nudge',
        title=f'{label}: please bring your records up to date',
        body=note or 'Your CRM hygiene score has records waiting.',
        url=where, entity_type='employee', entity_id=to,
        email=bool(body.get('email')), audit_action='hygiene.nudge',
        reason=note[:200] or 'Hygiene nudge')
    return jsonify(ok=True, **sent)


# ── pages ────────────────────────────────────────────────────────────
@bp.route('/hygiene')
@_signed_in
def page():
    sc = sc_mod.current()
    who, vertical = _args()
    try:
        data = hygiene.score(sc, emp_code=who or None,
                             vertical=vertical or None)
    except hygiene.OutsideScope as exc:
        return render_template('access_denied.html', need=str(exc)), 403
    return render_template(
        'hygiene/home.html', data=data, factor=None,
        levels=hygiene.levels(sc), verticals=hygiene.verticals_for(sc),
        viewer=session.get('emp_code'), who=who, vertical=vertical,
        can_nudge=(sc.unrestricted or len(sc.codes or ()) > 1))


@bp.route('/hygiene/factor/<key>')
@_signed_in
def factor_page(key):
    """The records behind one deduction — the whole point of the score.

    A number a person cannot argue with is a number they ignore, so
    every factor on the home page links here, and here lists exactly the
    records the checks flagged.
    """
    sc = sc_mod.current()
    who, vertical = _args()
    try:
        page_no = max(1, int(request.args.get('page') or 1))
    except ValueError:
        page_no = 1
    try:
        detail = hygiene.factor_records(sc, key, emp_code=who or None,
                                        vertical=vertical or None,
                                        page=page_no)
    except hygiene.OutsideScope as exc:
        return render_template('access_denied.html', need=str(exc)), 403
    if detail is None:
        return render_template('access_denied.html',
                               need='a factor that exists'), 404
    return render_template(
        'hygiene/home.html', data=None, factor=detail,
        levels=hygiene.levels(sc), verticals=hygiene.verticals_for(sc),
        viewer=session.get('emp_code'), who=who, vertical=vertical,
        can_nudge=(sc.unrestricted or len(sc.codes or ()) > 1))
