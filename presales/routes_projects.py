"""
v2026-08 — Pre-Sales Phase 2/3/4 HTTP surface.

Endpoints:

    GET  /api/projects                      list (role-filtered)
    POST /api/projects                      create
    GET  /api/projects/<id>                 detail (+ updates + accounts + contacts)
    PUT  /api/projects/<id>                 update fields
    POST /api/projects/<id>/stage           change stage (audit trail)
    POST /api/projects/<id>/updates         append intelligence update
    GET  /api/projects/<id>/updates         list updates newest-first
    POST /api/projects/<id>/accounts        link/unlink an Account with a role
    POST /api/projects/<id>/contacts        link/unlink a Contact
    POST /api/projects/<id>/convert         convert-to-Opportunity
    POST /api/accounts/<id>/convert         convert-to-Opportunity (Account-only)
    GET  /api/projects/vocab                enums for the UI
"""
from datetime import datetime, date
from flask import request, jsonify, session
from sqlalchemy.orm import joinedload

from app import (app, db, Company, Contact, Employee, Opportunity)
from presales import bp
from presales.models_projects import (
    Project, ProjectUpdate, ProjectStageHistory,
    ProjectAccount, ProjectContact, OpportunitySourceLink,
    PROJECT_STAGES, PROJECT_UPDATE_TYPES,
    PROJECT_ACCOUNT_ROLES, OPPORTUNITY_SOURCE_TYPES,
)
from presales import services_projects as svc
from presales.services import PreSalesError


# ─── auth helpers (same shape as routes.py) ────────────────────────────
def _require_login() -> bool:
    return bool(session.get('emp_code'))


def _current_emp():
    code = session.get('emp_code')
    return Employee.query.filter_by(emp_code=code).first() if code else None


def _visible_project_ids(emp: Employee):
    if emp.role == 'admin':
        return None
    if emp.is_vertical_head:
        subs = [emp.emp_code]
        for e in Employee.query.filter_by(vertical_head_id=emp.id).all():
            subs.append(e.emp_code)
        return {p.id for p in Project.query.filter(
            Project.pic_emp_code.in_(subs)).all()}
    return {p.id for p in Project.query.filter(
        Project.pic_emp_code == emp.emp_code).all()}


def _parse_date(s):
    if not s: return None
    for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y'):
        try: return datetime.strptime(s, fmt).date()
        except ValueError: continue
    return None


# ─── Project list + create ─────────────────────────────────────────────
@bp.route('/api/projects', methods=['GET'])
def api_projects_list():
    if not _require_login():
        return jsonify(ok=False, error='login required'), 401
    emp = _current_emp()
    q = Project.query.filter(Project.is_archived.is_(False))
    ids = _visible_project_ids(emp)
    if ids is not None:
        if not ids: return jsonify(ok=True, projects=[])
        q = q.filter(Project.id.in_(ids))
    # Filters
    for f, col in (('stage', Project.stage), ('pic', Project.pic_emp_code),
                   ('state', Project.state), ('industry', Project.industry),
                   ('priority', Project.priority),
                   ('vertical', Project.procam_vertical)):
        v = (request.args.get(f) or '').strip()
        if v: q = q.filter(col == v)
    s = (request.args.get('q') or '').strip()
    if s:
        q = q.filter(Project.name.ilike(f'%{s}%'))
    rows = q.order_by(Project.priority.desc(),
                      Project.last_update_at.desc().nullslast(),
                      Project.name.asc()).limit(500).all()
    return jsonify(ok=True, projects=[p.to_dict() for p in rows])


@bp.route('/api/projects', methods=['POST'])
def api_projects_create():
    if not _require_login():
        return jsonify(ok=False, error='login required'), 401
    emp = _current_emp()
    body = request.get_json(silent=True) or {}
    name = (body.get('name') or '').strip()
    if not name:
        return jsonify(ok=False, error='name required'), 400
    p = Project(
        name                = name,
        project_code        = (body.get('project_code') or '').strip() or None,
        project_type        = (body.get('project_type') or '').strip() or None,
        industry            = (body.get('industry') or '').strip() or None,
        country             = (body.get('country') or 'India').strip() or None,
        state               = (body.get('state') or '').strip() or None,
        location            = (body.get('location') or '').strip() or None,
        estimated_value_inr = body.get('estimated_value') or None,
        project_capacity    = (body.get('project_capacity') or '').strip() or None,
        announcement_date   = _parse_date(body.get('announcement_date')),
        expected_start_date = _parse_date(body.get('expected_start')),
        expected_proc_start = _parse_date(body.get('expected_proc_start')),
        expected_construction=(body.get('expected_construction') or '').strip() or None,
        stage               = (body.get('stage') or 'Project Identified'),
        procurement_status  = (body.get('procurement_status') or '').strip() or None,
        source              = (body.get('source') or '').strip() or None,
        source_url          = (body.get('source_url') or '').strip() or None,
        source_publication  = (body.get('source_publication') or '').strip() or None,
        source_date         = _parse_date(body.get('source_date')),
        description         = (body.get('description') or '').strip() or None,
        logistics_potential = (body.get('logistics_potential') or '').strip() or None,
        procam_vertical     = (body.get('procam_vertical') or '').strip() or None,
        priority            = (body.get('priority') or 'Medium'),
        pic_emp_code        = (body.get('pic') or emp.emp_code),
        branch              = (body.get('branch') or '').strip() or None,
        remarks             = (body.get('remarks') or '').strip() or None,
        created_by          = emp.emp_code,
    )
    db.session.add(p); db.session.flush()
    db.session.add(ProjectStageHistory(
        project_id=p.id, from_stage=None, to_stage=p.stage,
        changed_by=emp.emp_code, note='Initial stage on creation'))
    db.session.commit()
    return jsonify(ok=True, id=p.id), 201


# ─── Project detail + update ───────────────────────────────────────────
@bp.route('/api/projects/<int:pid>', methods=['GET'])
def api_projects_detail(pid):
    if not _require_login():
        return jsonify(ok=False, error='login required'), 401
    emp = _current_emp()
    p = Project.query.get_or_404(pid)
    ids = _visible_project_ids(emp)
    if ids is not None and p.id not in ids:
        return jsonify(ok=False, error='forbidden'), 403
    accounts = (db.session.query(ProjectAccount, Company)
                .join(Company, ProjectAccount.account_id == Company.id)
                .filter(ProjectAccount.project_id == p.id).all())
    contacts = (db.session.query(ProjectContact, Contact)
                .join(Contact, ProjectContact.contact_id == Contact.id)
                .filter(ProjectContact.project_id == p.id).all())
    updates = (ProjectUpdate.query.filter_by(project_id=p.id)
               .order_by(ProjectUpdate.update_date.desc(),
                         ProjectUpdate.id.desc()).limit(50).all())
    rfqs = (Opportunity.query.filter(Opportunity.source_project_id == p.id)
            .order_by(Opportunity.created_at.desc()).all())
    return jsonify(ok=True, project={
        **p.to_dict(),
        'accounts':  [{'id': c.id, 'name': c.name,
                       'role': pa.role, 'is_primary': pa.is_primary}
                      for pa, c in accounts],
        'contacts':  [{'id': c.id, 'name': c.name,
                       'designation': c.designation or '',
                       'email': c.email or '',
                       'role_on_project': pc.role_on_project or ''}
                      for pc, c in contacts],
        'updates':   [u.to_dict() for u in updates],
        'rfq_count': len(rfqs),
        'rfqs':      [{'id': o.id, 'opp_number': o.opp_number, 'title': o.title,
                       'stage': o.stage, 'value': float(o.value_inr or 0)}
                      for o in rfqs],
    })


@bp.route('/api/projects/<int:pid>', methods=['PUT'])
def api_projects_update(pid):
    if not _require_login():
        return jsonify(ok=False, error='login required'), 401
    emp = _current_emp()
    p = Project.query.get_or_404(pid)
    ids = _visible_project_ids(emp)
    if ids is not None and p.id not in ids:
        return jsonify(ok=False, error='forbidden'), 403
    b = request.get_json(silent=True) or {}
    for f in ('project_code','name','project_type','industry','country','state',
              'location','project_capacity','procurement_status','source',
              'source_url','source_publication','description',
              'logistics_potential','procam_vertical','priority','branch',
              'remarks','pic_emp_code'):
        if f in b:
            setattr(p, f, (b.get(f) or '').strip() or None)
    if 'estimated_value' in b:
        p.estimated_value_inr = b['estimated_value'] or None
    for f, k in (('announcement_date','announcement_date'),
                 ('expected_start_date','expected_start'),
                 ('expected_proc_start','expected_proc_start'),
                 ('source_date','source_date'),
                 ('next_review_at','next_review_at')):
        if k in b:
            setattr(p, f, _parse_date(b.get(k)))
    db.session.commit()
    return jsonify(ok=True)


# ─── Stage + timeline update ───────────────────────────────────────────
@bp.route('/api/projects/<int:pid>/stage', methods=['POST'])
def api_projects_stage(pid):
    if not _require_login():
        return jsonify(ok=False, error='login required'), 401
    emp = _current_emp()
    p = Project.query.get_or_404(pid)
    ids = _visible_project_ids(emp)
    if ids is not None and p.id not in ids:
        return jsonify(ok=False, error='forbidden'), 403
    b = request.get_json(silent=True) or {}
    try:
        svc.change_project_stage(project=p, new_stage=b.get('new_stage'),
                                 changed_by_code=emp.emp_code,
                                 note=b.get('note'))
        db.session.commit()
    except PreSalesError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True)


@bp.route('/api/projects/<int:pid>/updates', methods=['POST'])
def api_projects_update_create(pid):
    if not _require_login():
        return jsonify(ok=False, error='login required'), 401
    emp = _current_emp()
    p = Project.query.get_or_404(pid)
    ids = _visible_project_ids(emp)
    if ids is not None and p.id not in ids:
        return jsonify(ok=False, error='forbidden'), 403
    b = request.get_json(silent=True) or {}
    try:
        row = svc.log_project_update(
            project        = p,
            summary        = b.get('summary') or '',
            updated_by_code= emp.emp_code,
            update_type    = (b.get('update_type') or 'Other'),
            update_date    = _parse_date(b.get('update_date')),
            source         = b.get('source'),
            source_url     = b.get('source_url'),
            next_action    = b.get('next_action'),
            next_review_at = _parse_date(b.get('next_review_at')),
        )
        db.session.commit()
    except PreSalesError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, id=row.id), 201


@bp.route('/api/projects/<int:pid>/updates', methods=['GET'])
def api_projects_update_list(pid):
    if not _require_login():
        return jsonify(ok=False, error='login required'), 401
    emp = _current_emp()
    p = Project.query.get_or_404(pid)
    ids = _visible_project_ids(emp)
    if ids is not None and p.id not in ids:
        return jsonify(ok=False, error='forbidden'), 403
    rows = (ProjectUpdate.query.filter_by(project_id=p.id)
            .order_by(ProjectUpdate.update_date.desc(),
                      ProjectUpdate.id.desc()).limit(200).all())
    return jsonify(ok=True, updates=[r.to_dict() for r in rows])


# ─── Link / unlink Account or Contact ──────────────────────────────────
@bp.route('/api/projects/<int:pid>/accounts', methods=['POST'])
def api_projects_link_account(pid):
    if not _require_login():
        return jsonify(ok=False, error='login required'), 401
    emp = _current_emp()
    p = Project.query.get_or_404(pid)
    ids = _visible_project_ids(emp)
    if ids is not None and p.id not in ids and emp.role != 'admin':
        return jsonify(ok=False, error='forbidden'), 403
    b = request.get_json(silent=True) or {}
    op = (b.get('op') or 'add').lower()
    aid = b.get('account_id')
    role = (b.get('role') or 'Other')
    if not aid:
        return jsonify(ok=False, error='account_id required'), 400
    if op == 'remove':
        q = ProjectAccount.query.filter_by(project_id=p.id, account_id=aid)
        if b.get('role'):
            q = q.filter_by(role=role)
        n = q.delete()
        db.session.commit()
        return jsonify(ok=True, removed=n)
    account = Company.query.get(aid)
    if not account:
        return jsonify(ok=False, error='account not found'), 404
    try:
        svc.link_account_to_project(project=p, account=account, role=role,
                                    added_by_code=emp.emp_code,
                                    is_primary=bool(b.get('is_primary')),
                                    note=b.get('note'))
        db.session.commit()
    except PreSalesError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True)


@bp.route('/api/projects/<int:pid>/contacts', methods=['POST'])
def api_projects_link_contact(pid):
    if not _require_login():
        return jsonify(ok=False, error='login required'), 401
    emp = _current_emp()
    p = Project.query.get_or_404(pid)
    ids = _visible_project_ids(emp)
    if ids is not None and p.id not in ids and emp.role != 'admin':
        return jsonify(ok=False, error='forbidden'), 403
    b = request.get_json(silent=True) or {}
    cid = b.get('contact_id')
    if not cid:
        return jsonify(ok=False, error='contact_id required'), 400
    if (b.get('op') or 'add').lower() == 'remove':
        n = ProjectContact.query.filter_by(
            project_id=p.id, contact_id=cid).delete()
        db.session.commit()
        return jsonify(ok=True, removed=n)
    contact = Contact.query.get(cid)
    if not contact:
        return jsonify(ok=False, error='contact not found'), 404
    svc.link_contact_to_project(
        project=p, contact=contact,
        role_on_project=(b.get('role_on_project') or None),
        added_by_code=emp.emp_code)
    db.session.commit()
    return jsonify(ok=True)


# ─── Convert to Opportunity ────────────────────────────────────────────
@bp.route('/api/projects/<int:pid>/convert', methods=['POST'])
def api_projects_convert(pid):
    if not _require_login():
        return jsonify(ok=False, error='login required'), 401
    emp = _current_emp()
    p = Project.query.get_or_404(pid)
    ids = _visible_project_ids(emp)
    if ids is not None and p.id not in ids and emp.role != 'admin':
        return jsonify(ok=False, error='forbidden'), 403
    b = request.get_json(silent=True) or {}
    account = None
    if b.get('account_id'):
        account = Company.query.get(b['account_id'])
    try:
        opp = svc.convert_to_opportunity(
            account=account, project=p,
            title=b.get('title') or f'RFQ from {p.name}',
            source_type=b.get('source_type') or 'Project Intelligence',
            linked_by_code=emp.emp_code,
            value_inr=b.get('value_inr'),
            notes=b.get('notes'),
        )
        db.session.commit()
    except PreSalesError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, opportunity_id=opp.id), 201


@bp.route('/api/accounts/<int:aid>/convert', methods=['POST'])
def api_accounts_convert(aid):
    if not _require_login():
        return jsonify(ok=False, error='login required'), 401
    emp = _current_emp()
    account = Company.query.get_or_404(aid)
    b = request.get_json(silent=True) or {}
    project = None
    if b.get('project_id'):
        project = Project.query.get(b['project_id'])
    try:
        opp = svc.convert_to_opportunity(
            account=account, project=project,
            title=b.get('title') or f'RFQ from {account.name}',
            source_type=b.get('source_type') or 'Account Development',
            linked_by_code=emp.emp_code,
            value_inr=b.get('value_inr'),
            notes=b.get('notes'),
        )
        db.session.commit()
    except PreSalesError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, opportunity_id=opp.id), 201


# ─── Vocab endpoint ────────────────────────────────────────────────────
@bp.route('/api/projects/vocab', methods=['GET'])
def api_projects_vocab():
    if not _require_login():
        return jsonify(ok=False, error='login required'), 401
    return jsonify(ok=True,
        stages         = list(PROJECT_STAGES),
        update_types   = list(PROJECT_UPDATE_TYPES),
        account_roles  = list(PROJECT_ACCOUNT_ROLES),
        source_types   = list(OPPORTUNITY_SOURCE_TYPES),
    )
