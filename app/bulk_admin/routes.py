"""Bulk lead administration — §52-58.

Permission is split deliberately: bulk assignment and archiving are
ordinary admin work, permanent deletion is not (§55).
"""
from flask import Blueprint, jsonify, render_template, request, session

from app.access.service import require, require_super, is_super
from app.bulk_admin import service as bulk

bp = Blueprint('bulk_admin', __name__)

PERM = 'admin.master'
MAX_BULK = 2000


def _ids():
    d = request.get_json(silent=True) or {}
    ids = [int(i) for i in (d.get('ids') or []) if str(i).isdigit()]
    return ids[:MAX_BULK], d


@bp.route('/admin/leads')
@require(PERM)
def page():
    from app import Lead, Employee
    from app.master_data import service as md

    show = (request.args.get('show') or 'active').strip()
    q = Lead.query
    if show == 'archived':
        q = q.filter(Lead.is_archived.is_(True))
    else:
        q = q.filter(Lead.is_archived.isnot(True))

    if (request.args.get('unowned') or '') == '1':
        q = q.filter((Lead.assigned_to.is_(None)) | (Lead.assigned_to == ''))
    term = (request.args.get('q') or '').strip()
    if term:
        q = q.filter(Lead.company.ilike(f'%{term}%'))
    owner = (request.args.get('owner') or '').strip()
    if owner:
        q = q.filter(Lead.assigned_to == owner)

    total = q.count()
    rows = q.order_by(Lead.id.desc()).limit(300).all()

    return render_template(
        'bulk_admin/leads.html', leads=rows, total=total, show=show,
        term=term, owner=owner,
        unowned=(request.args.get('unowned') or '') == '1',
        employees=Employee.query.filter_by(is_active=True)
                          .order_by(Employee.name).all(),
        verticals=[i.label for i in md.items('vertical')],
        may_delete=is_super())


@bp.route('/api/bulk/leads/impact', methods=['POST'])
@require(PERM)
def api_impact():
    """§56 — what is attached, and whether deletion is allowed at all."""
    ids, _d = _ids()
    counts = bulk.linked_counts(ids)
    blocked = bulk.deletion_blocked(counts)
    return jsonify(ok=True, selected=len(ids), linked=counts,
                   delete_blocked=bool(blocked), reasons=blocked,
                   may_delete=is_super())


@bp.route('/api/bulk/leads/archive', methods=['POST'])
@require(PERM)
def api_archive():
    ids, d = _ids()
    if not ids:
        return jsonify(ok=False, error='Nothing selected'), 400
    return jsonify(ok=True, **bulk.archive(ids, d.get('reason', ''),
                                           session.get('emp_code')))


@bp.route('/api/bulk/leads/restore', methods=['POST'])
@require(PERM)
def api_restore():
    ids, _d = _ids()
    if not ids:
        return jsonify(ok=False, error='Nothing selected'), 400
    return jsonify(ok=True, **bulk.unarchive(ids, session.get('emp_code')))


@bp.route('/api/bulk/leads/assign', methods=['POST'])
@require(PERM)
def api_assign():
    ids, d = _ids()
    if not ids:
        return jsonify(ok=False, error='Nothing selected'), 400
    try:
        return jsonify(ok=True, **bulk.assign(ids, (d.get('emp_code') or ''),
                                              session.get('emp_code')))
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400


@bp.route('/api/bulk/leads/field', methods=['POST'])
@require(PERM)
def api_field():
    ids, d = _ids()
    if not ids:
        return jsonify(ok=False, error='Nothing selected'), 400
    try:
        return jsonify(ok=True, **bulk.set_field(
            ids, d.get('field') or '', d.get('value') or '',
            session.get('emp_code')))
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400


@bp.route('/api/bulk/leads/delete', methods=['POST'])
@require_super
def api_delete():
    """§55 — the super admin alone, and only where nothing material is
    attached."""
    ids, d = _ids()
    if not ids:
        return jsonify(ok=False, error='Nothing selected'), 400
    try:
        return jsonify(ok=True, **bulk.delete(ids, d.get('reason', ''),
                                              session.get('emp_code')))
    except PermissionError as exc:
        return jsonify(ok=False, error=str(exc), blocked=True), 409
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400


@bp.route('/admin/audit')
@require(PERM)
def audit_page():
    from app.models.audit import DeletionAudit
    rows = (DeletionAudit.query.order_by(DeletionAudit.id.desc())
            .limit(300).all())
    return render_template('bulk_admin/audit.html', rows=rows)
