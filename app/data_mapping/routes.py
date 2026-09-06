"""Data Mapping Review Queue — §65.

635 leads and contacts could not be linked to a company with certainty, so
they were queued rather than guessed.  This is where a person decides.

Rows are grouped by company name, because the 590 queued leads are only
about a hundred distinct names — resolving "Siemens Gamesa" once should
resolve every lead that carries it, not present the same decision twenty
times.
"""
from datetime import datetime

from flask import Blueprint, jsonify, request, session

from app import db
from app.access.service import require
from app.models.data_mapping import DataMappingQueue, MappingStatus
from app.services.company_match import norm

bp = Blueprint('data_mapping', __name__)

PERM = 'admin.master'


def _entity_model(entity_type):
    from app import Lead, Contact
    return {'Lead': Lead, 'Contact': Contact}.get(entity_type)


@bp.route('/api/mapping/queue')
@require(PERM)
def api_queue():
    """Pending decisions, grouped by the name that needs resolving."""
    reason = (request.args.get('reason') or '').strip()
    q = DataMappingQueue.query.filter_by(status=MappingStatus.PENDING)
    if reason:
        q = q.filter_by(reason=reason)

    groups = {}
    for row in q.order_by(DataMappingQueue.id).all():
        key = norm(row.raw_value) or (row.raw_value or '').lower()
        g = groups.setdefault(key, {
            'key': key, 'raw_value': row.raw_value,
            'reason': row.reason, 'candidates': row.candidates or [],
            'ids': [], 'entities': {},
        })
        g['ids'].append(row.id)
        g['entities'][row.entity_type] = g['entities'].get(
            row.entity_type, 0) + 1
        # Prefer whichever spelling carries suggestions.
        if not g['candidates'] and row.candidates:
            g['candidates'] = row.candidates

    ordered = sorted(groups.values(), key=lambda g: -len(g['ids']))
    return jsonify(
        ok=True,
        pending=DataMappingQueue.query.filter_by(
            status=MappingStatus.PENDING).count(),
        resolved=DataMappingQueue.query.filter_by(
            status=MappingStatus.RESOLVED).count(),
        groups=[{**g, 'count': len(g['ids'])} for g in ordered[:200]],
        total_groups=len(ordered))


@bp.route('/api/mapping/search')
@require(PERM)
def api_search():
    """Find a company to link a queued name to."""
    from app import Company
    term = (request.args.get('q') or '').strip()
    if len(term) < 2:
        return jsonify(ok=True, results=[])
    rows = (Company.query
            .filter(Company.is_active.is_(True),
                    Company.name.ilike(f'%{term}%'))
            .order_by(Company.name).limit(20).all())
    return jsonify(ok=True, results=[
        {'id': c.id, 'name': c.name,
         'meta': ' · '.join(x for x in (c.city, c.country, c.industry) if x)}
        for c in rows])


def _apply(rows, company_id, actor):
    """Point each queued entity at a company and close its queue row."""
    touched = 0
    for row in rows:
        model = _entity_model(row.entity_type)
        if model is None:
            continue
        entity = model.query.get(row.entity_id)
        if entity is not None:
            entity.company_id = company_id
            touched += 1
        row.status = MappingStatus.RESOLVED
        row.resolved_to = company_id
        row.resolved_by = actor
        row.resolved_at = datetime.utcnow()
    db.session.commit()
    return touched


@bp.route('/api/mapping/resolve', methods=['POST'])
@require(PERM)
def api_resolve():
    """Link every queued row sharing this name to one company.

    Either an existing `company_id`, or `create_name` to make a new
    Company Master record from the name as captured.
    """
    from app import Company
    d = request.get_json(silent=True) or {}
    ids = d.get('ids') or []
    if not ids:
        return jsonify(ok=False, error='Nothing selected'), 400

    rows = DataMappingQueue.query.filter(
        DataMappingQueue.id.in_(ids),
        DataMappingQueue.status == MappingStatus.PENDING).all()
    if not rows:
        return jsonify(ok=False, error='Those rows are already resolved'), 400

    company_id = d.get('company_id')
    if not company_id:
        name = (d.get('create_name') or '').strip()
        if not name:
            return jsonify(ok=False,
                           error='Pick a company or give a name to create'), 400
        # Guard against creating a duplicate of something that already
        # exists under a different spelling.
        from app.services.company_match import build_index, match
        index = build_index(
            Company.query.filter(Company.is_active.is_(True)).all())
        existing, reason, _c = match(name, index)
        if existing is not None:
            company_id = existing.id
        else:
            company = Company(name=name, is_active=True,
                              created_by=session.get('emp_code') or '')
            db.session.add(company)
            db.session.flush()
            company_id = company.id

    touched = _apply(rows, company_id, session.get('emp_code') or '')
    company = Company.query.get(company_id)
    return jsonify(ok=True, linked=touched, company_id=company_id,
                   company_name=company.name if company else '',
                   remaining=DataMappingQueue.query.filter_by(
                       status=MappingStatus.PENDING).count())


@bp.route('/api/mapping/skip', methods=['POST'])
@require(PERM)
def api_skip():
    """Set aside — the name is not a company we track."""
    d = request.get_json(silent=True) or {}
    rows = DataMappingQueue.query.filter(
        DataMappingQueue.id.in_(d.get('ids') or [])).all()
    for row in rows:
        row.status = MappingStatus.SKIPPED
        row.resolved_by = session.get('emp_code') or ''
        row.resolved_at = datetime.utcnow()
        row.note = (d.get('note') or '')[:500]
    db.session.commit()
    return jsonify(ok=True, skipped=len(rows),
                   remaining=DataMappingQueue.query.filter_by(
                       status=MappingStatus.PENDING).count())
