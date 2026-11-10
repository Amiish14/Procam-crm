"""Possible duplicates at creation, and an admin merge that reverses.

CRM-WA-01 §2.

    GET  /api/leads/duplicate-check          while the form is open
    GET  /api/leads/<id>/duplicates          for a lead already on file
    GET  /admin/lead-duplicates              the admin screen
    GET  /api/admin/lead-duplicates          the same, as JSON
    POST /api/admin/lead-duplicates/merge    {lead_id, into, reason}
    POST /api/admin/lead-duplicates/unmerge  {lead_id}

The duplicate check is open to anybody signed in: it is the warning
shown while a lead is being keyed in, and the person keying it in is
usually not an administrator. It returns only what the leads list
already shows them — number, company, stage, owner, date — and
nothing about leads outside their scope is created or changed by it.

Merging and reversing are admin only. §2 asks for "an admin Merge",
and the reason is sound: the person keying a lead in is not always
the person who can tell whether two enquiries are one job, and a
merge moves every activity, mail and quote off one record.

There is a separate screen for accounts — /admin/account-duplicates,
`app/account_merge` — and this is not it. One customer with two
account rows and one enquiry entered twice are different problems
with different fixes, and a screen that mixed them would offer the
wrong button half the time.
"""
from __future__ import annotations

from functools import wraps

from flask import (Blueprint, abort, jsonify, render_template, request,
                   session)

bp = Blueprint('lead_dupes', __name__)


def _me():
    return session.get('emp_code')


def _dupes(f):
    """Signed in. The warning, which everybody keying a lead in needs.

    Its own decorator so the route audit names the gate. It returns
    only the columns the leads list already shows, and it writes
    nothing.
    """
    @wraps(f)
    def wrap(*a, **kw):
        if not _me():
            return jsonify(ok=False, error='Sign in first.'), 401
        return f(*a, **kw)
    return wrap


def _dupe_admin(f):
    """Admin, through the Access Matrix. Merging moves other people's
    records off a lead, which is why it is not the lead's own owner."""
    @wraps(f)
    def wrap(*a, **kw):
        if not _me():
            return jsonify(ok=False, error='Sign in first.'), 401
        try:
            from app.access.service import can
            allowed = bool(can('admin.triage') or can('admin.master'))
        except Exception:                                 # noqa: BLE001
            allowed = False
        if not allowed:
            return jsonify(
                ok=False,
                error='Only an administrator can merge leads.'), 403
        return f(*a, **kw)
    return wrap


# ── the warning ──────────────────────────────────────────────────────
@bp.route('/api/leads/duplicate-check')
@_dupes
def duplicate_check():
    """Four signals, each with its own strength and its own sentence.

    Asked with whatever the form has so far, so it can be called
    while somebody is still typing.
    """
    from app.services import lead_duplicates as ld

    rows = ld.check(
        company=request.args.get('company'),
        email=request.args.get('email'),
        phone=request.args.get('phone'),
        gstin=request.args.get('gstin'),
        exclude_id=request.args.get('exclude_id', type=int),
        within_days=request.args.get('within_days', type=int))
    return jsonify(ok=True, rows=rows, count=len(rows),
                   signals=[{'key': k, 'strength': s, 'what': w}
                            for k, s, w in ld.SIGNALS])


@bp.route('/api/leads/<int:lid>/duplicates')
@_dupes
def duplicates_for(lid):
    from app import _require_lead_access
    from app.services import lead_duplicates as ld
    from app.services import lead_merge

    lead = _require_lead_access(lid)
    ledger = lead_merge.last_merge(lead)
    return jsonify(ok=True, rows=ld.for_lead(lead),
                   merged_into=getattr(lead, 'merged_into_id', None),
                   merge=(ledger.to_dict() if ledger is not None
                          else None),
                   unmerge_blocked=lead_merge.unmerge_blockers(lead))


# ── the admin screen ─────────────────────────────────────────────────
@bp.route('/admin/lead-duplicates')
@_dupe_admin
def page():
    return render_template('admin/lead_duplicates.html')


@bp.route('/api/admin/lead-duplicates')
@_dupe_admin
def api_pairs():
    """Every live pair worth a person's attention, strongest first."""
    from app.services import lead_dupe_report as report

    limit = request.args.get('limit', type=int) or 100
    within = request.args.get('within_days', type=int)
    pairs = report.pairs(limit=limit, within_days=within)
    return jsonify(ok=True, pairs=pairs, count=len(pairs))


@bp.route('/api/admin/lead-duplicates/merge', methods=['POST'])
@_dupe_admin
def api_merge():
    from app import Lead, db
    from app.services import lead_merge

    body = request.get_json(silent=True) or {}
    lead = db.session.get(Lead, body.get('lead_id') or 0)
    into = db.session.get(Lead, body.get('into') or 0)
    if lead is None or into is None:
        return jsonify(ok=False,
                       error='One of those leads does not exist.'), 404
    ok, detail = lead_merge.merge(lead, into, actor=_me(),
                                 reason=(body.get('reason') or '').strip())
    if not ok:
        return jsonify(ok=False, error=detail), 400
    return jsonify(ok=True, **detail)


@bp.route('/api/admin/lead-duplicates/unmerge', methods=['POST'])
@_dupe_admin
def api_unmerge():
    """Put a merged lead and its rows back where they came from."""
    from app import Lead, db
    from app.services import lead_merge

    body = request.get_json(silent=True) or {}
    lead = db.session.get(Lead, body.get('lead_id') or 0)
    if lead is None:
        return jsonify(ok=False,
                       error='That lead does not exist.'), 404
    ok, detail = lead_merge.unmerge(lead, actor=_me())
    if not ok:
        return jsonify(ok=False, error=detail), 400
    return jsonify(ok=True, **detail)
