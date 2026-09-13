"""Operations status — for administrators.

    GET /admin/ops         the page
    GET /api/ops/status    the same, as JSON

The slow and privileged checks (quick_check on the database and the
newest backup, Graph, the TLS handshake, ps, systemctl, git, the schema
comparison) run in scripts/ops_status.py from a systemd timer, which
writes a JSON report. This blueprint only reads that file, and adds the
few checks a web worker can run on a page load without holding the
worker: row counts and ages from indexed queries on a read-only
connection.
"""
import os

from flask import Blueprint, jsonify, render_template

from app.access.service import require
from app.ops import checks

bp = Blueprint('ops', __name__)

PERM = 'admin.access'

_PILL = {checks.OK: 'won', checks.WARN: 'warn', checks.FAIL: 'lost',
         checks.UNKNOWN: 'muted'}


def _db_path():
    """The file the running app actually uses. Read from the engine, not
    DATABASE_URL: Flask-SQLAlchemy resolves a relative SQLite path
    against the instance folder."""
    from app import db
    url = db.engine.url
    if not url.drivername.startswith('sqlite'):
        return ''
    path = url.database or ''
    if path.startswith('file:'):
        path = path[len('file:'):].split('?', 1)[0]
    return '' if path in ('', ':memory:') else os.path.abspath(path)


def _status():
    stored = checks.read_status(checks.default_status_path())
    ctx = checks.Context(db_path=_db_path(), network=False, schema=False)
    return stored, checks.live_checks(ctx)


def _age_text(seconds):
    if seconds is None:
        return ''
    if seconds < 90:
        return f'{seconds} s'
    if seconds < 5400:
        return f'{seconds // 60} min'
    if seconds < 172800:
        return f'{seconds // 3600} h'
    return f'{seconds // 86400} days'


@bp.route('/admin/ops')
@require(PERM)
def page():
    stored, live = _status()
    return render_template('ops/status.html', stored=stored, live=live,
                           pill=_PILL, age_text=_age_text(
                               stored['age_seconds']),
                           stale_minutes=checks.STATUS_STALE_MIN)


@bp.route('/api/ops/status')
@require(PERM)
def api_status():
    stored, live = _status()
    return jsonify(ok=True, stored=stored, live=live,
                   live_overall=checks.worst(r['status'] for r in live))
