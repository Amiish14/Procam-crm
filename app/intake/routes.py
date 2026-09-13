"""
The three intake screens.

    GET  /accounts/owners          Account Master — two owners per account
    GET  /lead-review              what the classifier could not decide
    GET  /intake-intelligence      whether any of this is working

Admin-only, enforced on the server through the access matrix. Each screen
has a small JSON API behind it so the page can act without reloading.
"""
from flask import Blueprint, jsonify, render_template, request, session

from app import db
from app.access.service import require
from app.intake import service as svc


bp = Blueprint('intake', __name__)

_PERM = 'admin.triage'      # same audience as Lead Triage


def _actor():
    return session.get('emp_code')


# ─── Account Master ──────────────────────────────────────────────────────
@bp.route('/accounts/owners')
@require(_PERM)
def accounts_page():
    from app import Employee
    people = (Employee.query
              .filter(Employee.is_active.is_(True))
              .order_by(Employee.name).all())
    verticals = []
    try:
        from app.master_data import service as md
        verticals = [i.label for i in md.items('vertical')]
    except Exception:
        pass
    return render_template('intake/accounts.html',
                           people=[{'code': e.emp_code, 'name': e.name}
                                   for e in people],
                           verticals=verticals)


@bp.route('/api/intake/accounts', methods=['GET'])
@require(_PERM)
def api_accounts():
    return jsonify(ok=True,
                   summary=svc.account_summary(),
                   accounts=svc.account_rows(
                       missing_only=request.args.get('missing') == '1',
                       q=request.args.get('q')))


@bp.route('/api/intake/accounts/<int:company_id>', methods=['PUT'])
@require(_PERM)
def api_save_account(company_id):
    d = request.get_json(silent=True) or {}

    clashes = svc.domain_conflicts(company_id, d.get('domains', ''))
    if clashes and not d.get('confirm_domains'):
        return jsonify(ok=False, domain_conflict=True, clashes=clashes,
                       error='Another account already claims that domain. '
                             'A lead from it would go to whichever the '
                             'lookup reached first.'), 409

    ok, err = svc.save_account_owners(
        company_id, primary=d.get('primary'), secondary=d.get('secondary'),
        backup=d.get('backup'), vertical=d.get('vertical'),
        domains=d.get('domains'), actor=_actor())
    if not ok:
        db.session.rollback()
        return jsonify(ok=False, error=err), 400
    db.session.commit()
    return jsonify(ok=True, summary=svc.account_summary())


# ─── Lead Review ─────────────────────────────────────────────────────────
@bp.route('/lead-review')
@require(_PERM)
def review_page():
    from app import Employee
    from app.services import lead_intake as li
    from app.services import lead_assignment
    people = (Employee.query
              .filter(Employee.is_active.is_(True))
              .order_by(Employee.name).all())
    return render_template(
        'intake/review.html',
        reasons=svc.rejection_reasons(),
        classes=[{'key': k, 'label': v} for k, v in li.Klass.LABELS.items()],
        people=[{'code': e.emp_code, 'name': e.name} for e in people],
        reassign_reasons=lead_assignment.REASSIGNMENT_REASONS)


@bp.route('/api/intake/review', methods=['GET'])
@require(_PERM)
def api_review():
    return jsonify(ok=True, counts=svc.review_counts(),
                   items=svc.review_queue())


@bp.route('/api/intake/review/<int:cid>/accept', methods=['POST'])
@require(_PERM)
def api_accept(cid):
    row, err = svc.accept(cid, actor=_actor())
    if err:
        db.session.rollback()
        return jsonify(ok=False, error=err), 400
    db.session.commit()
    return jsonify(ok=True, lead_id=row.created_lead_id,
                   counts=svc.review_counts())


@bp.route('/api/intake/review/<int:cid>/reject', methods=['POST'])
@require(_PERM)
def api_reject(cid):
    d = request.get_json(silent=True) or {}
    row, err = svc.reject(cid, reason=d.get('reason'), actor=_actor(),
                          note=d.get('note'))
    if err:
        db.session.rollback()
        return jsonify(ok=False, error=err), 400
    db.session.commit()
    return jsonify(ok=True, counts=svc.review_counts())


@bp.route('/api/intake/review/<int:cid>/reclassify', methods=['POST'])
@require(_PERM)
def api_reclassify(cid):
    d = request.get_json(silent=True) or {}
    row, err = svc.reclassify(cid, to_class=d.get('to_class'),
                              reason=d.get('reason'), actor=_actor())
    if err:
        db.session.rollback()
        return jsonify(ok=False, error=err), 400
    db.session.commit()
    return jsonify(ok=True, counts=svc.review_counts())


@bp.route('/api/intake/review/<int:cid>/merge', methods=['POST'])
@require(_PERM)
def api_merge(cid):
    """§21 — attach this email to a lead that already exists."""
    d = request.get_json(silent=True) or {}
    row, err = svc.merge(cid, lead_id=d.get('lead_id'), actor=_actor())
    if err:
        db.session.rollback()
        return jsonify(ok=False, error=err), 400
    db.session.commit()
    return jsonify(ok=True, lead_id=row.matched_lead_id,
                   counts=svc.review_counts())


@bp.route('/api/intake/review/<int:cid>/reassign', methods=['POST'])
@require(_PERM)
def api_reassign(cid):
    """§21 — put the lead this email produced with the right person."""
    d = request.get_json(silent=True) or {}
    row, err = svc.reassign(cid, primary_code=d.get('primary'),
                            secondary_code=d.get('secondary'),
                            reason=d.get('reason'), actor=_actor())
    if err:
        db.session.rollback()
        return jsonify(ok=False, error=err), 400
    db.session.commit()
    return jsonify(ok=True,
                   lead_id=row.created_lead_id or row.matched_lead_id,
                   counts=svc.review_counts())


# ─── Learning — §11, Phase 3 ─────────────────────────────────────────────
@bp.route('/api/intake/proposals', methods=['GET'])
@require(_PERM)
def api_proposals():
    from app.intake import learning
    return jsonify(ok=True, proposals=learning.proposals())


@bp.route('/api/intake/proposals/apply', methods=['POST'])
@require(_PERM)
def api_apply_proposal():
    from app.intake import learning
    key = (request.get_json(silent=True) or {}).get('key') or ''
    ok, msg = learning.apply_proposal(key, actor=_actor())
    if not ok:
        db.session.rollback()
        return jsonify(ok=False, error=msg), 400
    db.session.commit()
    return jsonify(ok=True, message=msg)


@bp.route('/api/intake/proposals/dismiss', methods=['POST'])
@require(_PERM)
def api_dismiss_proposal():
    from app.intake import learning
    key = (request.get_json(silent=True) or {}).get('key') or ''
    ok, err = learning.dismiss_proposal(key, actor=_actor())
    if not ok:
        return jsonify(ok=False, error=err), 400
    return jsonify(ok=True)


# ─── Intelligence ────────────────────────────────────────────────────────
@bp.route('/intake-intelligence')
@require(_PERM)
def intelligence_page():
    return render_template('intake/intelligence.html')


@bp.route('/api/intake/intelligence', methods=['GET'])
@require(_PERM)
def api_intelligence():
    try:
        days = max(1, min(int(request.args.get('days') or 30), 365))
    except (TypeError, ValueError):
        days = 30
    return jsonify(ok=True, **svc.intelligence(days=days))
