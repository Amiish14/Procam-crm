"""
Daily Workbench — the API and the two pages.

    /my-work                     the Daily Workbench (the daily screen)
    /team-workbench              the same board across the people a
                                 manager's data scope reaches
    /api/workbench               summary + a page of action items
    /api/workbench/quote-ageing  the RFQ/quote ageing buckets
    /api/workbench/bulk/preview  what a bulk action would change
    /api/workbench/bulk/apply    do it, with a reason, per record
    /api/workbench/remind        send a logged reminder to a colleague

Visibility is the Access Matrix's, through `app.access.scope`: the board
is built from scoped queries, so the Team Workbench shows exactly the
people a manager's scope reaches and nothing else. The `for` parameter
narrows within that scope; it can never widen it.
"""
from __future__ import annotations

from functools import wraps

from flask import (Blueprint, jsonify, redirect, render_template, request,
                   session, url_for)

from app.access import scope as sc_mod
from app.services import sales_rules as rules
from app.workbench import bulk as wb_bulk
from app.workbench import service as wb

bp = Blueprint('workbench', __name__)


def _emp():
    from app import Employee
    code = session.get('emp_code')
    return Employee.query.filter_by(emp_code=code).first() if code else None


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


def _scope_for_request():
    """The viewer's scope, optionally narrowed to one colleague.

    `?for=CODE` narrows; it never widens. A code outside the viewer's
    scope is refused rather than silently ignored, so a manager can tell
    the difference between "nothing to do" and "not yours to see".
    """
    sc = sc_mod.current()
    who = (request.args.get('for') or '').strip().upper()
    if not who or who == (session.get('emp_code') or '').upper():
        return sc, None
    if not sc.reaches(who) and not sc.unrestricted:
        return None, (jsonify(ok=False,
                              error='That person is outside your access'), 403)
    narrowed = sc_mod.Scope(sc.emp_code, sc.vertical, {who}, sc.perms,
                            sc.data_scope)
    return narrowed, None


@bp.route('/api/workbench')
@_signed_in
def api_workbench():
    sc, refused = _scope_for_request()
    if refused:
        return refused
    data = wb.board(
        sc,
        group=(request.args.get('group') or '').strip() or None,
        search=(request.args.get('q') or '').strip(),
        vertical=(request.args.get('vertical') or '').strip(),
        stage=(request.args.get('stage') or '').strip(),
        owner=(request.args.get('owner') or '').strip(),
        page=request.args.get('page', 1),
        per_page=request.args.get('per_page', 50),
        sort=(request.args.get('sort') or 'priority').strip())
    data['ok'] = True
    data['viewer'] = session.get('emp_code')
    data['scope'] = 'company' if sc.unrestricted else sc.data_scope
    return jsonify(data)


@bp.route('/api/workbench/quote-ageing')
@_signed_in
def api_quote_ageing():
    sc, refused = _scope_for_request()
    if refused:
        return refused
    ageing = rules.rfq_ageing(sc=sc)
    bucket = (request.args.get('bucket') or '').strip()
    rows = (ageing['overdue'] if bucket == 'overdue'
            else ageing['buckets'].get(bucket, []) if bucket
            else [r for rs in ageing['buckets'].values() for r in rs])
    return jsonify(ok=True, total=ageing['total'],
                   overdue_count=ageing['overdue_count'],
                   buckets=[{'key': k, 'label': lbl,
                             'count': ageing['counts'].get(k, 0)}
                            for k, lbl, _lo, _hi in rules.AGEING_BUCKETS],
                   rows=rows[:500])


def _refused(exc):
    return jsonify(ok=False, error=exc.message, **exc.extra), exc.status


@bp.route('/api/workbench/bulk/preview', methods=['POST'])
@_signed_in
def api_bulk_preview():
    body = request.get_json(silent=True) or {}
    try:
        out = wb_bulk.preview(body.get('ids'), (body.get('action') or '').strip(),
                              body.get('value'), sc=sc_mod.current(),
                              actor=session.get('emp_code'))
    except wb_bulk.BulkRefused as exc:
        return _refused(exc)
    return jsonify(ok=True, **out)


@bp.route('/api/workbench/bulk/apply', methods=['POST'])
@_signed_in
def api_bulk_apply():
    body = request.get_json(silent=True) or {}
    try:
        out = wb_bulk.apply(body.get('ids'), (body.get('action') or '').strip(),
                            body.get('value'), body.get('reason'),
                            body.get('preview_token'), sc=sc_mod.current(),
                            actor=session.get('emp_code'))
    except wb_bulk.BulkRefused as exc:
        return _refused(exc)
    return jsonify(ok=True, **out)


@bp.route('/api/workbench/remind', methods=['POST'])
@_signed_in
def api_remind():
    """A manager nudges somebody their scope reaches. Logged, never silent."""
    if not session.get('emp_code'):
        return _need_login()
    from app import Employee
    from app.services import notify

    body = request.get_json(silent=True) or {}
    to = (body.get('to') or '').strip().upper()
    message = (body.get('message') or '').strip()
    group = (body.get('group') or '').strip()
    sc = sc_mod.current()
    if not to:
        return jsonify(ok=False, error='Say who the reminder is for'), 400
    if to == (session.get('emp_code') or '').upper():
        return jsonify(ok=False, error='That is you'), 400
    if not sc.unrestricted and not sc.reaches(to):
        return jsonify(ok=False,
                       error='That person is outside your access'), 403
    emp = Employee.query.filter_by(emp_code=to, is_active=True).first()
    if emp is None:
        return jsonify(ok=False, error='No such active employee'), 404
    if len(message) > 500:
        return jsonify(ok=False, error='Keep the reminder under 500 characters'), 400
    link = '/my-work' + (f'?group={group}' if group else '')
    sent = notify.send(
        to, kind='workbench_reminder',
        title=f'Reminder from {session.get("name") or session.get("emp_code")}',
        body=message or 'Please bring your Workbench up to date.',
        url=link, entity_type='employee', entity_id=to,
        email=bool(body.get('email')),
        audit_action='workbench.reminder',
        reason=message[:200] or 'Workbench reminder')
    return jsonify(ok=True, **sent)


# ── pages ────────────────────────────────────────────────────────────
@bp.route('/my-work')
@_signed_in
def workbench_page():
    emp = _emp()
    return render_template('workbench/home.html', emp=emp, team=False,
                           groups=rules.GROUPS,
                           buckets=rules.AGEING_BUCKETS)


@bp.route('/team-workbench')
@_signed_in
def team_page():
    emp = _emp()
    sc = sc_mod.current()
    if sc.unrestricted:
        people = None
    else:
        people = sorted(sc.codes or set())
        if len(people) <= 1:
            return render_template('access_denied.html',
                                   need='a team to look after'), 403
    return render_template('workbench/home.html', emp=emp, team=True,
                           groups=rules.GROUPS,
                           buckets=rules.AGEING_BUCKETS)


@bp.route('/api/workbench/people')
@_signed_in
def api_people():
    """Who the viewer may filter the Team Workbench by."""
    if not session.get('emp_code'):
        return _need_login()
    from app import Employee
    sc = sc_mod.current()
    q = Employee.query.filter(Employee.is_active.is_(True))
    if not sc.unrestricted:
        q = q.filter(Employee.emp_code.in_(sc.codes or {''}))
    return jsonify(ok=True, people=[
        {'emp_code': e.emp_code, 'name': e.name or e.emp_code,
         'vertical': e.vertical or ''}
        for e in q.order_by(Employee.name.asc()).limit(500)])
