"""Global CRM — the page and its API.

    /global-crm                               the screen
    /api/directory/contacts                   scoped search, filtered in SQL
    /api/directory/contact/<id>               one person's whole relationship
    /api/directory/dashboard                  the shape of the database
    /api/directory/contact/<id>/assign        change who looks after them
    /api/accounts/who-handles                 Group D: who owns this account?

Every route needs a session; what each one then shows is decided per
record by the Access Matrix, through ``app.access.scope``. The write
endpoint additionally needs ``accounts.assign`` and refuses with the
``need=`` key the access-denied screen reads.

``/api/accounts/who-handles`` sits under the accounts namespace rather
than the directory one because it answers an account question, and
because the Copilot will call it there when it learns to route "who
handles X?" — one endpoint, not a second copy of the rule.
"""
from __future__ import annotations

from functools import wraps

from flask import (Blueprint, abort, jsonify, redirect, render_template,
                   request, session, url_for)

from app.access import scope as sc_mod
from app.directory import service as svc

bp = Blueprint('directory', __name__)


def _need_login():
    return jsonify(ok=False, error='Not authenticated'), 401


def _signed_in(f):
    """A session is the floor. Everything above it is the Access Matrix,
    applied per record rather than per route."""
    @wraps(f)
    def wrap(*a, **kw):
        if not session.get('emp_code'):
            if request.path.startswith('/api/'):
                return _need_login()
            return redirect(url_for('login'))
        return f(*a, **kw)
    return wrap


def _arg(name, default=None):
    value = (request.args.get(name) or '').strip()
    return value or default


def _refused(exc):
    return jsonify(ok=False, error=exc.message, **exc.extra), exc.status


# ── search ───────────────────────────────────────────────────────────
@bp.route('/api/directory/contacts')
@_signed_in
def api_contacts():
    sc = sc_mod.current()
    data = svc.search_contacts(
        sc,
        q=_arg('q'), company=_arg('company'), designation=_arg('designation'),
        industry=_arg('industry'), country=_arg('country'), city=_arg('city'),
        vertical=_arg('vertical'), account_id=_arg('account_id'),
        pic=_arg('pic'), relationship=_arg('relationship'),
        service=_arg('service'),
        last_interaction_days=_arg('last_interaction_days'),
        page=request.args.get('page', 1),
        per_page=request.args.get('per_page', svc.DEFAULT_PER_PAGE))
    data['ok'] = True
    data['scope'] = 'company' if sc.unrestricted else sc.data_scope
    return jsonify(data)


@bp.route('/api/directory/dashboard')
@_signed_in
def api_dashboard():
    return jsonify(ok=True, **svc.dashboard(sc_mod.current()))


@bp.route('/api/directory/contact/<int:cid>')
@_signed_in
def api_contact(cid):
    data = svc.relationship_view(sc_mod.current(), cid)
    if data is None:
        # 404, not 403: see service.relationship_view().
        abort(404)
    return jsonify(ok=True, **data)


@bp.route('/api/directory/contact/<int:cid>/assign', methods=['POST'])
@_signed_in
def api_assign(cid):
    body = request.get_json(silent=True) or {}

    def field(key):
        return body[key] if key in body else svc.UNCHANGED

    try:
        out = svc.assign(sc_mod.current(), cid,
                         primary_pic=field('primary_pic'),
                         secondary_pic=field('secondary_pic'),
                         vertical=field('vertical'),
                         account_id=field('account_id'),
                         relationship_type=field('relationship_type'),
                         reason=body.get('reason'),
                         actor=session.get('emp_code'))
    except svc.DirectoryRefused as exc:
        from app import db
        db.session.rollback()
        return _refused(exc)
    return jsonify(ok=True, **out)


@bp.route('/api/accounts/who-handles')
@_signed_in
def api_who_handles():
    return jsonify(ok=True, **svc.who_handles(sc_mod.current(), _arg('q', ''),
                                              limit=request.args.get('limit', 20)))


# ── the page ─────────────────────────────────────────────────────────
@bp.route('/global-crm')
@_signed_in
def global_crm_page():
    from app import Employee

    sc = sc_mod.current()
    svc.ensure_tables()
    filters = {k: _arg(k, '') for k in
               ('q', 'company', 'designation', 'industry', 'country', 'city',
                'vertical', 'account_id', 'pic', 'relationship', 'service',
                'last_interaction_days')}
    results = svc.search_contacts(
        sc, page=request.args.get('page', 1),
        per_page=request.args.get('per_page', svc.DEFAULT_PER_PAGE),
        **filters)
    people = (sc_mod.employees(sc=sc)
              .filter(Employee.is_active.is_(True))
              .order_by(Employee.name.asc()).limit(500).all())
    return render_template(
        'directory/home.html',
        emp=Employee.query.filter_by(emp_code=sc.emp_code).first(),
        filters=filters, results=results,
        dash=svc.dashboard(sc),
        vocab=svc.vocabularies(),
        may_assign=sc.can(svc.ASSIGN_PERM),
        assign_perm=svc.ASSIGN_PERM,
        people=[{'code': e.emp_code, 'name': e.name or e.emp_code}
                for e in people])
