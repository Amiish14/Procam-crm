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
from app.models.help_content import (HelpArticle, HelpGuide,
                                     HelpGuideShown, HelpTicket,
                                     HelpTooltip)
from app.services.urls import prefixed as _prefixed, login_url as _login_url


bp = Blueprint('help_v2', __name__)


# ─── auth helpers ────────────────────────────────────────────────────────
def _require_auth(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not session.get('emp_code'):
            return jsonify(error='Not authenticated'), 401
        return f(*a, **kw)
    return wrap


def _current_emp():
    return session.get('emp_code') or ''


def _current_role():
    r = session.get('role') or ''
    return r.strip().lower()


def _is_admin():
    """Maintaining help content is master-data work in the Access Matrix
    (it used to be the role name, which the matrix could not narrow)."""
    from app.access.service import can
    return can('admin.master')


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
        return ('', 302, {'Location': _login_url()})
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


# ─── Guided help (CRM-LEAD-FLOW-01 §5) ──────────────────────────────────
#
# "No screen may show only an error." The catalogue is served to
# every page, the admin screen edits the wording without a deploy,
# and each showing is logged so the Thursday pack can say which
# guides people actually use.
@bp.route('/api/help/guides')
@_require_auth
def api_guides():
    from app.services import guided_help
    return jsonify(ok=True, guides=guided_help.catalogue(),
                   enabled=guided_help.enabled())


@bp.route('/api/help/guides/<code>')
@_require_auth
def api_guide(code):
    from app.services import guided_help
    found = guided_help.guide(code, request.args.to_dict())
    guided_help.record(found['code'], emp_code=_current_emp(),
                       lead_id=request.args.get('lead_id', type=int),
                       page=request.args.get('page', ''), commit=True)
    return jsonify(ok=True, guide=found)


@bp.route('/api/help/guides/<code>/clicked', methods=['POST'])
@_require_auth
def api_guide_clicked(code):
    """The person pressed one of the guide's own buttons.

    Recorded against the most recent showing rather than as a new
    row, so "shown" and "acted on" count the same event.
    """
    from app import db
    from app.services import guided_help

    body = request.get_json(silent=True) or {}
    label = (body.get('label') or '')[:60]
    row = (HelpGuideShown.query
           .filter_by(code=(code or '').strip().upper(),
                      emp_code=_current_emp())
           .order_by(HelpGuideShown.id.desc()).first())
    if row is None:
        guided_help.record(code, emp_code=_current_emp(),
                           lead_id=body.get('lead_id'),
                           page=body.get('page', ''), clicked=label)
    else:
        row.clicked = label or 'button'
    db.session.commit()
    return jsonify(ok=True)


@bp.route('/admin/help-texts')
@_require_admin
def admin_help_texts():
    from app.services import guided_help
    return render_template('admin/help_texts.html',
                           guides=guided_help.catalogue(
                               include_inactive=True),
                           weekly=guided_help.weekly())


@bp.route('/api/admin/help-guides', methods=['POST'])
@_require_admin
def api_save_guide():
    """Create or rewrite one guide. The code is never editable —
    it is what the code refers to."""
    from app import db
    from app.services import guided_help

    body = request.get_json(silent=True) or {}
    code = (body.get('code') or '').strip().upper()
    if not code:
        return jsonify(ok=False, error='A guide needs its code.'), 400
    title = (body.get('title') or '').strip()
    if not title:
        return jsonify(ok=False,
                       error='A guide with no title tells nobody '
                             'anything.'), 400

    row = HelpGuide.query.filter_by(code=code).first()
    if row is None:
        row = HelpGuide(code=code)
        db.session.add(row)
    row.title = title[:200]
    row.why = (body.get('why') or '').strip()
    steps = body.get('steps')
    if isinstance(steps, str):
        steps = [s.strip() for s in steps.splitlines() if s.strip()]
    row.steps = list(steps or [])
    if isinstance(body.get('actions'), list):
        row.actions = body['actions']
    elif not row.actions:
        row.actions = list(
            guided_help.SEED_BY_CODE.get(code, {}).get('actions', []))
    row.audience = (body.get('audience') or '').strip()[:40]
    row.active = bool(body.get('active', True))
    row.last_edited_by = _current_emp()
    db.session.commit()
    return jsonify(ok=True, guide=row.to_dict())


@bp.route('/api/admin/help-guides/seed', methods=['POST'])
@_require_admin
def api_seed_guides():
    from app.services import guided_help
    added = guided_help.seed(actor=_current_emp())
    return jsonify(ok=True, added=added,
                   total=len(guided_help.catalogue(
                       include_inactive=True)))


@bp.route('/api/help/ticket', methods=['POST'])
@_require_auth
def api_raise_ticket():
    """§5.2 — Ask for help. Answers with the ticket number."""
    from app.services import guided_help

    body = request.get_json(silent=True) or {}
    row, error = guided_help.raise_ticket(
        emp_code=_current_emp(), page=(body.get('page') or ''),
        lead_id=body.get('lead_id'), code=(body.get('code') or ''),
        description=(body.get('description') or ''))
    if row is None:
        return jsonify(ok=False, error=error), 400
    return jsonify(ok=True, number=row.number, ticket=row.to_dict())


@bp.route('/api/help/tickets')
@_require_admin
def api_tickets():
    rows = (HelpTicket.query.order_by(HelpTicket.id.desc())
            .limit(200).all())
    return jsonify(ok=True, rows=[r.to_dict() for r in rows])


@bp.route('/api/help/people')
@_require_auth
def api_people():
    """Active employees, once each (§3.4).

    The team filter listed people who had left and showed one name
    twice. Both come from reading the employee master without
    filtering it, so this is the filtered reading every new screen
    uses.
    """
    from app import Employee

    rows = (Employee.query.filter(Employee.is_active.is_(True))
            .order_by(Employee.name.asc()).all())
    seen, out = set(), []
    for emp in rows:
        code = (emp.emp_code or '').strip()
        if not code or code in seen:
            continue
        seen.add(code)
        out.append({'emp_code': code, 'name': emp.name or code,
                    'vertical': emp.vertical or ''})
    return jsonify(ok=True, rows=out, total=len(out))
