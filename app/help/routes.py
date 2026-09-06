"""Self-Help / User Manual — routes (Phase 13).

HTML
    GET  /help                       — full manual, role-filtered
    GET  /help/<slug>                — single article
    GET  /admin/help                 — admin CRUD hub

JSON
    GET  /api/help/tooltips?page=X   — tooltips for a given page

Admin JSON
    POST   /api/admin/help/articles              create
    PATCH  /api/admin/help/articles/<id>         update
    DELETE /api/admin/help/articles/<id>         delete
    GET    /api/admin/help/articles              list
    POST   /api/admin/help/tooltips              create
    PATCH  /api/admin/help/tooltips/<id>         update
    DELETE /api/admin/help/tooltips/<id>         delete
    GET    /api/admin/help/tooltips              list
"""
from datetime import datetime
from functools import wraps

from flask import (Blueprint, jsonify, render_template, request, session,
                   abort)

from app import db
from app.models.help_content import HelpArticle, HelpTooltip


bp = Blueprint('help_v2', __name__)


# ─── auth helpers ────────────────────────────────────────────────────────
def _require_auth(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not session.get('emp_code'):
            return jsonify(error='Not authenticated'), 401
        return f(*a, **kw)
    return wrap


def _current_role():
    r = session.get('role') or ''
    return r.strip().lower()


def _is_admin():
    return _current_role() in ('admin', 'administrator')


def _require_admin(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not session.get('emp_code'):
            return jsonify(error='Not authenticated'), 401
        if not _is_admin():
            return jsonify(error='Admin only'), 403
        return f(*a, **kw)
    return wrap


def _visible(row):
    """True if row.role_visibility is empty or contains current role."""
    roles = [str(r).lower() for r in (row.role_visibility or [])]
    if not roles:
        return True
    return _current_role() in roles or _is_admin()


# ─── HTML — user-facing manual ───────────────────────────────────────────
@bp.route('/help')
def help_index():
    articles = (HelpArticle.query
                .filter_by(is_active=True)
                .order_by(HelpArticle.section, HelpArticle.display_order,
                          HelpArticle.title)
                .all())
    visible = [a for a in articles if _visible(a)]
    # Group by section for the sidebar.
    sections = {}
    for a in visible:
        sections.setdefault(a.section, []).append(a)
    return render_template('help/index.html', sections=sections,
                           articles=visible)


@bp.route('/help/<slug>')
def help_article(slug):
    a = HelpArticle.query.filter_by(slug=slug).first_or_404()
    if not a.is_active or not _visible(a):
        abort(404)
    return render_template('help/article.html', article=a)


# ─── JSON — tooltips ─────────────────────────────────────────────────────
@bp.route('/api/help/tooltips', methods=['GET'])
def api_tooltips():
    page = (request.args.get('page') or '').strip()
    if not page:
        return jsonify(ok=True, tooltips={})
    q = HelpTooltip.query.filter(HelpTooltip.page == page,
                                 HelpTooltip.is_active.is_(True))
    out = {}
    for t in q.all():
        if not _visible(t):
            continue
        out[t.element_key] = t.tooltip_html
    return jsonify(ok=True, tooltips=out)


# ─── Admin — HTML ────────────────────────────────────────────────────────
@bp.route('/admin/help')
def admin_hub():
    if not session.get('emp_code'):
        return ('', 302, {'Location': '/login'})
    if not _is_admin():
        abort(403)
    articles = (HelpArticle.query
                .order_by(HelpArticle.section, HelpArticle.display_order)
                .all())
    tooltips = (HelpTooltip.query
                .order_by(HelpTooltip.page, HelpTooltip.element_key)
                .all())
    return render_template('help/admin.html',
                           articles=articles, tooltips=tooltips)


# ─── Admin — JSON: articles ──────────────────────────────────────────────
@bp.route('/api/admin/help/articles', methods=['GET'])
@_require_admin
def api_articles_list():
    rows = HelpArticle.query.order_by(HelpArticle.section,
                                      HelpArticle.display_order).all()
    return jsonify(ok=True, articles=[r.to_dict() for r in rows])


@bp.route('/api/admin/help/articles', methods=['POST'])
@_require_admin
def api_articles_create():
    data = request.get_json(silent=True) or {}
    slug = (data.get('slug') or '').strip().lower()
    section = (data.get('section') or '').strip().lower()
    title = (data.get('title') or '').strip()
    if not slug or not section or not title:
        return jsonify(error='slug, section, title required'), 400
    if HelpArticle.query.filter_by(slug=slug).first():
        return jsonify(error='slug already exists'), 409
    row = HelpArticle(
        slug=slug, section=section, title=title,
        what_it_is=data.get('what_it_is'),
        when_to_use=data.get('when_to_use'),
        how_to_use=data.get('how_to_use'),
        required_fields=data.get('required_fields'),
        what_happens_next=data.get('what_happens_next'),
        common_mistakes=data.get('common_mistakes'),
        role_visibility=list(data.get('role_visibility') or []),
        display_order=int(data.get('display_order') or 0),
        is_active=bool(data.get('is_active', True)),
        updated_by_id=session.get('emp_code'),
    )
    db.session.add(row)
    db.session.commit()
    return jsonify(ok=True, article=row.to_dict())


@bp.route('/api/admin/help/articles/<int:aid>', methods=['PATCH'])
@_require_admin
def api_articles_update(aid):
    row = HelpArticle.query.get_or_404(aid)
    data = request.get_json(silent=True) or {}
    for f in ('section', 'title', 'what_it_is', 'when_to_use',
              'how_to_use', 'required_fields', 'what_happens_next',
              'common_mistakes'):
        if f in data:
            setattr(row, f, data[f])
    if 'role_visibility' in data:
        row.role_visibility = list(data['role_visibility'] or [])
    if 'display_order' in data:
        row.display_order = int(data['display_order'] or 0)
    if 'is_active' in data:
        row.is_active = bool(data['is_active'])
    row.updated_by_id = session.get('emp_code')
    row.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify(ok=True, article=row.to_dict())


@bp.route('/api/admin/help/articles/<int:aid>', methods=['DELETE'])
@_require_admin
def api_articles_delete(aid):
    row = HelpArticle.query.get_or_404(aid)
    db.session.delete(row)
    db.session.commit()
    return jsonify(ok=True)


# ─── Admin — JSON: tooltips ──────────────────────────────────────────────
@bp.route('/api/admin/help/tooltips', methods=['GET'])
@_require_admin
def api_tooltips_list():
    rows = HelpTooltip.query.order_by(HelpTooltip.page,
                                      HelpTooltip.element_key).all()
    return jsonify(ok=True, tooltips=[r.to_dict() for r in rows])


@bp.route('/api/admin/help/tooltips', methods=['POST'])
@_require_admin
def api_tooltips_create():
    data = request.get_json(silent=True) or {}
    page = (data.get('page') or '').strip()
    key  = (data.get('element_key') or '').strip()
    html = (data.get('tooltip_html') or '').strip()
    if not page or not key or not html:
        return jsonify(error='page, element_key, tooltip_html required'), 400
    row = HelpTooltip(
        page=page, element_key=key, tooltip_html=html,
        role_visibility=list(data.get('role_visibility') or []),
        is_active=bool(data.get('is_active', True)),
    )
    db.session.add(row)
    db.session.commit()
    return jsonify(ok=True, tooltip=row.to_dict())


@bp.route('/api/admin/help/tooltips/<int:tid>', methods=['PATCH'])
@_require_admin
def api_tooltips_update(tid):
    row = HelpTooltip.query.get_or_404(tid)
    data = request.get_json(silent=True) or {}
    for f in ('page', 'element_key', 'tooltip_html'):
        if f in data:
            setattr(row, f, data[f])
    if 'role_visibility' in data:
        row.role_visibility = list(data['role_visibility'] or [])
    if 'is_active' in data:
        row.is_active = bool(data['is_active'])
    row.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify(ok=True, tooltip=row.to_dict())


@bp.route('/api/admin/help/tooltips/<int:tid>', methods=['DELETE'])
@_require_admin
def api_tooltips_delete(tid):
    row = HelpTooltip.query.get_or_404(tid)
    db.session.delete(row)
    db.session.commit()
    return jsonify(ok=True)
