"""
Notification inbox + bell (Phase 5 of the CRM upgrade).

Copied from Procam-lr-main/app/notifications/routes.py.  Adaptations
for CRM:

  * Auth via session['emp_code'] (CRM has no Flask-Login).
  * `user_id` is a String (emp_code) not an int.

Routes:
    GET  /notifications                inbox
    POST /notifications/<id>/read      mark one read (redirects)
    POST /notifications/read-all       mark all read
    GET  /notifications/api/unread     JSON count + top 10 for the bell dropdown
"""
from datetime import datetime

from flask import (Blueprint, render_template, redirect, url_for, jsonify,
                   request, session, current_app)

from app import db
from app.models.notification import Notification


bp = Blueprint('notifications', __name__)


def _current_emp_code():
    return session.get('emp_code')


@bp.route('/notifications')
def inbox():
    emp_code = _current_emp_code()
    if not emp_code:
        return redirect(url_for('login'))
    rows = (Notification.query
            .filter(Notification.user_id == emp_code)
            .order_by(Notification.created_at.desc())
            .limit(200).all())
    return render_template('notifications/inbox.html', rows=rows)


@bp.route('/notifications/<int:nid>/read', methods=['POST'])
def mark_read(nid):
    emp_code = _current_emp_code()
    if not emp_code:
        return jsonify(ok=False, error='login required'), 401
    n = Notification.query.get_or_404(nid)
    if n.user_id != emp_code:
        return jsonify(ok=False, error='forbidden'), 403
    if not n.is_read:
        n.is_read = True
        n.read_at = datetime.utcnow()
        db.session.commit()
    return redirect(n.action_url or url_for('notifications.inbox'))


@bp.route('/notifications/read-all', methods=['POST'])
def read_all():
    emp_code = _current_emp_code()
    if not emp_code:
        return jsonify(ok=False, error='login required'), 401
    Notification.query.filter(
        Notification.user_id == emp_code,
        Notification.is_read.is_(False),
    ).update({'is_read': True, 'read_at': datetime.utcnow()},
             synchronize_session=False)
    db.session.commit()
    return redirect(url_for('notifications.inbox'))


@bp.route('/notifications/api/unread')
def unread():
    """Bell-dropdown feed. Bounded, defensive — this endpoint is polled
    by every open tab every 5 min.  Never let it 502 the topbar."""
    emp_code = _current_emp_code()
    if not emp_code:
        return jsonify(unread=0, items=[]), 200
    from sqlalchemy import text as _text
    try:
        if db.engine.dialect.name == 'postgresql':
            try:
                db.session.execute(_text("SET LOCAL statement_timeout = '5s'"))
            except Exception:
                pass
        q = (Notification.query
             .filter(Notification.user_id == emp_code,
                     Notification.is_read.is_(False))
             .order_by(Notification.created_at.desc()))
        top = q.limit(10).all()
        badge = len(top)
        if badge == 10:
            try:
                badge = min(q.count(), 99)
            except Exception:
                badge = 10
        return jsonify(
            unread=badge,
            items=[{
                'id':      n.id,
                'kind':    n.kind,
                'title':   n.title,
                'body':    (n.body or '')[:120],
                'url':     n.action_url or '/notifications',
                'age_min': int((datetime.utcnow() - n.created_at).total_seconds() / 60),
            } for n in top],
        )
    except Exception:
        try:
            current_app.logger.exception('notifications.unread failed')
        except Exception:
            pass
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify(unread=0, items=[]), 200
