"""Data Quality dashboard — §66."""
from flask import Blueprint, jsonify, render_template

from app.access.service import require
from app.data_quality import service as dq

bp = Blueprint('data_quality', __name__)


@bp.route('/admin/data-quality')
@require('admin.master')
def page():
    checks = dq.summary()
    return render_template(
        'data_quality/index.html', checks=checks,
        critical=[c for c in checks if c['severity'] == 'critical'
                  and (c['count'] or 0) > 0],
        total=sum(c['count'] or 0 for c in checks))


@bp.route('/api/data-quality')
@require('admin.master')
def api():
    return jsonify(ok=True, checks=dq.summary())


@bp.route('/admin/data-quality/<key>')
@require('admin.master')
def detail(key):
    """The records behind one number."""
    data = dq.records_for(key)
    if data is None:
        return render_template('data_quality/index.html',
                               checks=dq.summary(), critical=[], total=0), 404
    return render_template('data_quality/detail.html', **data)


@bp.route('/api/data-quality/<key>')
@require('admin.master')
def api_detail(key):
    data = dq.records_for(key)
    if data is None:
        return jsonify(ok=False, error=f'Unknown check "{key}"'), 404
    return jsonify(ok=True, **data)
