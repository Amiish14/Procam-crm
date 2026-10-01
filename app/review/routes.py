"""
Sales review — the pages and the API.

    /review/individual            one person's review
    /review/vertical              one desk's review
    /review/meeting               the agenda, in the order it is run
    /api/review/individual        the same figures, as JSON
    /api/review/vertical
    /api/review/meeting
    GET  /api/review/actions      the actions on a review scope
    POST /api/review/actions      write down what was agreed
    POST /api/review/actions/<id>/close   done, or carried forward

Every route needs a session and is then scope-checked against its
subject: a manager may review the people their Access Matrix boundary
reaches and nobody else. Asking about somebody outside it is refused,
never answered with an empty review — "nothing to show" and "not yours
to see" must not look the same.
"""
from __future__ import annotations

from functools import wraps

from flask import (Blueprint, jsonify, redirect, render_template, request,
                   session)

from app.access import scope as sc_mod
from app.review import service as rv
from app.services import sales_rules as rules
from app.services.urls import login_url as _login_url

bp = Blueprint('review', __name__)


def _signed_in(f):
    """A session is the floor; the Access Matrix decides the rest, per
    subject, inside each view."""
    @wraps(f)
    def wrap(*a, **kw):
        if not session.get('emp_code'):
            if request.path.startswith('/api/'):
                return jsonify(ok=False, error='Not authenticated'), 401
            return redirect(_login_url())
        return f(*a, **kw)
    return wrap


def _refused(exc, *, page=False):
    if page:
        return render_template('access_denied.html',
                               need=exc.message), exc.status
    return jsonify(ok=False, error=exc.message), exc.status


def _arg(name, default=''):
    return (request.args.get(name) or default).strip()


def _subject_code():
    """Whose individual review this is — the viewer's own by default."""
    return (_arg('emp') or session.get('emp_code') or '').upper()


# ── API ──────────────────────────────────────────────────────────────
@bp.route('/api/review/individual')
@_signed_in
def api_individual():
    try:
        data = rv.individual(_subject_code(), _arg('start'), _arg('end'),
                             sc=sc_mod.current())
    except rv.ReviewRefused as exc:
        return _refused(exc)
    return jsonify(ok=True, **data)


@bp.route('/api/review/vertical')
@_signed_in
def api_vertical():
    try:
        data = rv.vertical(_arg('vertical'), _arg('start'), _arg('end'),
                           sc=sc_mod.current())
    except rv.ReviewRefused as exc:
        return _refused(exc)
    return jsonify(ok=True, **data)


@bp.route('/api/review/meeting')
@_signed_in
def api_meeting():
    try:
        data = rv.meeting(_scope_key(), _arg('period', 'week'),
                          sc=sc_mod.current(),
                          start=_arg('start'), end=_arg('end'))
    except rv.ReviewRefused as exc:
        return _refused(exc)
    return jsonify(ok=True, **data)


def _scope_key():
    """The review this request is about.

    Given explicitly as `scope`, or assembled from the friendlier `emp` /
    `vertical` parameters the pages use.
    """
    explicit = _arg('scope')
    if explicit:
        return explicit
    if _arg('vertical'):
        return f"vertical:{_arg('vertical')}"
    return f'emp:{_subject_code()}'


@bp.route('/api/review/actions')
@_signed_in
def api_actions():
    try:
        rows = rv.actions(_scope_key(), sc=sc_mod.current(),
                          status=_arg('status'), owner=_arg('owner'),
                          subject=_arg('subject'))
    except rv.ReviewRefused as exc:
        return _refused(exc)
    return jsonify(ok=True, actions=rows, scope=_scope_key())


@bp.route('/api/review/actions', methods=['POST'])
@_signed_in
def api_create_action():
    body = request.get_json(silent=True) or {}
    try:
        action = rv.create_action(
            body.get('review_scope') or _scope_key(),
            body.get('description'),
            owner_emp_code=body.get('owner_emp_code') or '',
            subject_emp_code=body.get('subject_emp_code') or '',
            due_date=body.get('due_date'),
            linked_entity_type=body.get('linked_entity_type') or '',
            linked_entity_id=body.get('linked_entity_id'),
            actor=session.get('emp_code'), sc=sc_mod.current())
    except rv.ReviewRefused as exc:
        return _refused(exc)
    return jsonify(ok=True, action=action.to_dict()), 201


@bp.route('/api/review/actions/<int:action_id>/close', methods=['POST'])
@_signed_in
def api_close_action(action_id):
    body = request.get_json(silent=True) or {}
    try:
        action, successor = rv.close_action(
            action_id,
            outcome=(body.get('outcome') or 'done').strip().lower(),
            note=body.get('note') or '', due_date=body.get('due_date'),
            actor=session.get('emp_code'), sc=sc_mod.current())
    except rv.ReviewRefused as exc:
        return _refused(exc)
    return jsonify(ok=True, action=action.to_dict(),
                   carried_to=successor.to_dict() if successor else None)


# ── pages ────────────────────────────────────────────────────────────
@bp.route('/review/individual')
@_signed_in
def individual_page():
    try:
        data = rv.individual(_subject_code(), _arg('start'), _arg('end'),
                             sc=sc_mod.current())
    except rv.ReviewRefused as exc:
        return _refused(exc, page=True)
    return render_template('review/individual.html', rv=data,
                           people=_people_in_scope(), rules=rules)


@bp.route('/review/vertical')
@_signed_in
def vertical_page():
    sc = sc_mod.current()
    choices = _verticals_in_scope(sc)
    name = _arg('vertical') or (choices[0] if choices else '')
    try:
        data = rv.vertical(name, _arg('start'), _arg('end'), sc=sc)
    except rv.ReviewRefused as exc:
        return _refused(exc, page=True)
    return render_template('review/vertical.html', rv=data,
                           verticals=choices, rules=rules)


@bp.route('/review/meeting')
@_signed_in
def meeting_page():
    try:
        data = rv.meeting(_scope_key(), _arg('period', 'week'),
                          sc=sc_mod.current(),
                          start=_arg('start'), end=_arg('end'))
    except rv.ReviewRefused as exc:
        return _refused(exc, page=True)
    return render_template('review/meeting.html', rv=data,
                           people=_people_in_scope(),
                           periods=rv.PERIODS, rules=rules)


def _people_in_scope():
    """Who the viewer may pick as the subject of a review."""
    from app import Employee
    sc = sc_mod.current()
    q = Employee.query.filter(Employee.is_active.is_(True))
    if not sc.unrestricted:
        q = q.filter(Employee.emp_code.in_(sc.codes or {''}))
    return [{'emp_code': e.emp_code, 'name': e.name or e.emp_code,
             'vertical': e.vertical or ''}
            for e in q.order_by(Employee.name.asc()).limit(500)]


def _verticals_in_scope(sc):
    """The desks the viewer may review. Their own unless they see
    everything — the same answer `may_review_vertical` gives."""
    from app import Employee
    if not sc.unrestricted:
        return [sc.vertical] if rv.may_review_vertical(sc.vertical, sc) else []
    rows = (Employee.query.with_entities(Employee.vertical)
            .filter(Employee.vertical.isnot(None),
                    Employee.is_active.is_(True)).distinct().all())
    return sorted({(r[0] or '').strip() for r in rows if (r[0] or '').strip()})
