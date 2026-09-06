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
