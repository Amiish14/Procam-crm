"""Master Data Center — the admin screen's API (§59-62).

Reading a list needs only a session, because forms and filters everywhere
depend on it.  Changing one needs admin.master.
"""
from flask import Blueprint, jsonify, request, session

from app.access.service import require
from app.master_data import service as md

bp = Blueprint('master_data', __name__)

PERM = 'admin.master'


@bp.route('/api/master/lists')
def api_lists():
    """Every list with its items — drives the admin screen and the Excel
    lookup sheets (§45)."""
    if not session.get('emp_code'):
        return jsonify(ok=False, error='Not authenticated'), 401
    return jsonify(ok=True, lists=md.all_lists())


@bp.route('/api/master/<list_key>')
def api_list(list_key):
    """One vocabulary.  Any signed-in user: forms need this."""
    if not session.get('emp_code'):
        return jsonify(ok=False, error='Not authenticated'), 401
    inactive = (request.args.get('all') or '') in ('1', 'true', 'yes')
    return jsonify(ok=True, key=list_key,
                   items=[i.to_dict() for i in
                          md.items(list_key, include_inactive=inactive)])


@bp.route('/api/master/<list_key>', methods=['POST'])
@require(PERM)
def api_add(list_key):
    d = request.get_json(silent=True) or {}
    try:
        item = md.add_item(list_key,
                           code=d.get('code') or d.get('label'),
                           label=d.get('label'),
                           description=d.get('description', ''),
                           meta=d.get('meta'),
                           actor=session.get('emp_code'))
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    return jsonify(ok=True, item=item.to_dict())


@bp.route('/api/master/item/<int:item_id>', methods=['PUT'])
@require(PERM)
def api_update(item_id):
    d = request.get_json(silent=True) or {}
    try:
        item = md.update_item(item_id, **d)
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 404
    return jsonify(ok=True, item=item.to_dict())


@bp.route('/api/master/item/<int:item_id>', methods=['DELETE'])
@require(PERM)
def api_delete(item_id):
    """§61 — a value with history is retired, not removed."""
    try:
        deleted, used = md.delete_item(item_id)
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 404
    if deleted:
        return jsonify(ok=True, deleted=True)
    return jsonify(ok=True, deleted=False, deactivated=True, used_by=used,
                   message=f'{used} record(s) still use this value, so it '
                           f'was deactivated instead of deleted. '
                           f'Historical data is unchanged.')


@bp.route('/api/master/item/<int:item_id>/usage')
@require(PERM)
def api_usage(item_id):
    from app.models.master_data import MasterItem
    item = MasterItem.query.get(item_id)
    if item is None:
        return jsonify(ok=False, error='No such item'), 404
    return jsonify(ok=True, code=item.code, list_key=item.list_key,
                   used_by=md.usage_count(item.list_key, item.code))
