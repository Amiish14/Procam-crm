"""
FINISH-TRACKER-CRM §0 — merge duplicate accounts from one screen.

    GET  /admin/account-duplicates          the clusters, as a page
    GET  /api/admin/account-duplicates      the same, as JSON
    POST /api/admin/account-duplicates/merge   fold losers into one

Every route 404s while FEATURE_ACCOUNT_MERGE is off, so nothing here
exists for anybody outside the pilot. A merge is the hardest change
in the CRM to see afterwards, so the screen shows the whole cluster —
lead counts, wins, owners per vertical, the identifiers — and the
survivor is a radio button, never a default that gets agreed to.

Nothing is deleted. The loser keeps its row, marked inactive and
renamed "<name> (merged into #id)", and its spelling becomes an
alias of the survivor so the old name still reaches the right
customer.
"""
from __future__ import annotations

import importlib.util
import os
from functools import wraps

from flask import Blueprint, jsonify, render_template, request, session

bp = Blueprint('account_merge', __name__)

FLAG = 'FEATURE_ACCOUNT_MERGE'


def enabled():
    try:
        from app.services import flags
        return bool(flags.on_for(FLAG, session.get('emp_code')))
    except Exception:                                     # noqa: BLE001
        return False


def _dupes():
    """The script, loaded as a module.

    The reading of what makes two accounts one customer lives in
    `scripts/account_duplicates.py` and is used by both the script
    and this screen, so the page and the command line can never
    disagree about what a duplicate is.
    """
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))),
        'scripts', 'account_duplicates.py')
    spec = importlib.util.spec_from_file_location(
        'account_duplicates', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _me():
    return session.get('emp_code')


def _gate(f):
    """404 unless the flag is on for this person."""
    @wraps(f)
    def wrap(*a, **kw):
        if not _me():
            return jsonify(ok=False, error='Not authenticated'), 401
        if not enabled():
            return jsonify(ok=False, error='Not found'), 404
        return f(*a, **kw)
    return wrap


def _admin_only(f):
    @wraps(f)
    def wrap(*a, **kw):
        from app import Employee

        if not _me():
            from flask import redirect, url_for
            return redirect(url_for('login'))
        emp = Employee.query.filter_by(emp_code=_me()).first()
        role = (getattr(emp, 'role', '') or '').lower()
        if role not in ('admin', 'super admin', 'superadmin',
                        'director'):
            return jsonify(ok=False,
                           error='Admins and directors only.'), 403
        return f(*a, **kw)
    return wrap


def _rows(found):
    """One cluster, as the page and the API both want it."""
    from app.services import rfq_board

    out = []
    for cluster in found:
        accounts = []
        for row in cluster['accounts']:
            owners = [{'vertical': v or '(none)', 'emp_code': c,
                       'name': rfq_board.name_of(c)}
                      for v, c in sorted(row['owners'].items())]
            accounts.append({
                'id': row['id'], 'name': row['name'],
                'leads': row['leads'], 'open_leads': row['open_leads'],
                'won_leads': row['won_leads'],
                'gstin': row['gstin'], 'domains': row['domains'],
                'vertical': row['vertical'],
                'owner': row['owner'],
                'owner_name': rfq_board.name_of(row['owner']),
                'owners': owners,
                'suggested': row['id'] == cluster['survivor']['id'],
            })
        out.append({
            'name': cluster['survivor']['name'],
            'survivor': cluster['survivor']['id'],
            'survivor_why': cluster['survivor_why'],
            'reasons': cluster['reasons'],
            'leads': cluster['leads'],
            'open_leads': cluster['open_leads'],
            'accounts': accounts,
        })
    return out


@bp.route('/admin/account-duplicates')
@_gate
@_admin_only
def page():
    rows = _rows(_dupes().clusters())
    return render_template('admin/account_duplicates.html', rows=rows,
                           total=len(rows))


@bp.route('/api/admin/account-duplicates')
@_gate
@_admin_only
def api_list():
    rows = _rows(_dupes().clusters())
    return jsonify(ok=True, rows=rows, total=len(rows))


@bp.route('/api/admin/account-duplicates/merge', methods=['POST'])
@_gate
@_admin_only
def api_merge():
    body = request.get_json(silent=True) or {}
    survivor = body.get('survivor')
    losers = body.get('merge') or []
    if not survivor or not losers:
        return jsonify(ok=False,
                       error='Name the account to keep and at least '
                             'one to fold into it.'), 400
    try:
        survivor = int(survivor)
        losers = [int(x) for x in losers]
    except (TypeError, ValueError):
        return jsonify(ok=False, error='Account ids must be '
                                       'numbers.'), 400
    if survivor in losers:
        return jsonify(ok=False, error='The survivor cannot also be '
                                       'merged away.'), 400

    done, refused = _dupes().merge(survivor, losers, actor=_me())
    if not done:
        why = refused[0][1] if refused else 'nothing to merge'
        return jsonify(ok=False, error=why, refused=[
            {'account_id': a, 'why': w} for a, w in refused]), 400
    return jsonify(ok=True, merged=[
        {'account_id': row['loser'], 'was': row['was'],
         'moved': sum(row['moved'].values()),
         'left_behind': sum(row['left_behind'].values())}
        for row in done], refused=[
        {'account_id': a, 'why': w} for a, w in refused])
