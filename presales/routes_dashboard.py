"""
v2026-08 — Pre-Sales Phase 5 & 6 · Dashboard summary + Reports.

Endpoints:

    GET /api/presales/dashboard/summary      cards + charts payload
    GET /api/presales/reports/accounts.csv   Account Development Register export
    GET /api/presales/reports/projects.csv   Project Intelligence Register export
    GET /api/presales/reports/attribution.csv  RFQ-source attribution export
"""
import csv
import io
from datetime import datetime, timedelta, date
from flask import request, jsonify, session, Response
from sqlalchemy import func

from app import (app, db, Company, Contact, Employee, Opportunity)
from presales import bp
from presales.models import (
    AccountActivity, AccountRelationshipTag, AccountAssignmentHistory,
    AccountStageHistory, ACCOUNT_DEV_STAGES,
)
from presales.models_projects import (
    Project, ProjectUpdate, ProjectAccount, ProjectContact,
    OpportunitySourceLink, PROJECT_STAGES,
)


def _require_login() -> bool:
    return bool(session.get('emp_code'))


def _current_emp():
    code = session.get('emp_code')
    return Employee.query.filter_by(emp_code=code).first() if code else None


def _scoped_accounts(emp: Employee):
    q = Company.query.filter(Company.is_active.is_(True))
    if emp.role == 'admin':
        return q
    if emp.is_vertical_head:
        subs = [emp.emp_code] + [e.emp_code for e in
                Employee.query.filter_by(vertical_head_id=emp.id).all()]
        return q.filter(Company.pic_emp_code.in_(subs))
    return q.filter(Company.pic_emp_code == emp.emp_code)


def _scoped_projects(emp: Employee):
    q = Project.query.filter(Project.is_archived.is_(False))
    if emp.role == 'admin':
        return q
    if emp.is_vertical_head:
        subs = [emp.emp_code] + [e.emp_code for e in
                Employee.query.filter_by(vertical_head_id=emp.id).all()]
        return q.filter(Project.pic_emp_code.in_(subs))
    return q.filter(Project.pic_emp_code == emp.emp_code)


# ─── Dashboard summary ────────────────────────────────────────────────
@bp.route('/api/presales/dashboard/summary', methods=['GET'])
def api_presales_dashboard():
    if not _require_login():
        return jsonify(ok=False, error='login required'), 401
    emp = _current_emp()
    acc_q = _scoped_accounts(emp)
    prj_q = _scoped_projects(emp)

    now = datetime.utcnow()
    d7  = now - timedelta(days=7)
    d15 = now - timedelta(days=15)
    d30 = now - timedelta(days=30)

    total_accounts   = acc_q.count()
    strategic        = acc_q.filter(Company.strategic_flag.is_(True)).count()
    active_accounts  = acc_q.filter(
        Company.last_activity_at.isnot(None),
        Company.last_activity_at >= d30).count()
    dormant          = acc_q.filter(
        (Company.last_activity_at.is_(None)) |
        (Company.last_activity_at < d30)).count()

    total_projects   = prj_q.count()
    high_priority    = prj_q.filter(Project.priority == 'High').count()
    epc_tendering    = prj_q.filter(Project.stage == 'EPC Tendering').count()
    epc_appointed    = prj_q.filter(Project.stage == 'EPC Appointed').count()
    proc_started     = prj_q.filter(Project.stage == 'Procurement Started').count()
    rfq_expected     = prj_q.filter(Project.stage == 'RFQ Expected').count()

    # Charts — Account by stage
    by_stage = dict(db.session.query(Company.dev_stage, func.count(Company.id))
                    .filter(Company.id.in_([a.id for a in acc_q.all()]))
                    .group_by(Company.dev_stage).all())
    # Charts — Project by stage
    proj_by_stage = dict(db.session.query(Project.stage, func.count(Project.id))
                         .filter(Project.id.in_([p.id for p in prj_q.all()]))
                         .group_by(Project.stage).all())
    # Top accounts by RFQ source
    top_accounts = (db.session.query(
        Company.name, func.count(Opportunity.id).label('rfqs'))
        .join(Opportunity, Opportunity.source_account_id == Company.id)
        .filter(Company.id.in_([a.id for a in acc_q.all()]) if emp.role != 'admin' else True)
        .group_by(Company.name)
        .order_by(func.count(Opportunity.id).desc())
        .limit(10).all())

    return jsonify(ok=True,
        cards={
            'accounts_total':      total_accounts,
            'strategic_accounts':  strategic,
            'active_last_30d':     active_accounts,
            'dormant':             dormant,
            'projects_total':      total_projects,
            'high_priority':       high_priority,
            'epc_tendering':       epc_tendering,
            'epc_appointed':       epc_appointed,
            'procurement_started': proc_started,
            'rfq_expected':        rfq_expected,
        },
        charts={
            'account_by_stage':  [{'stage': s or 'Unclassified', 'count': n}
                                   for s, n in by_stage.items()],
            'project_by_stage':  [{'stage': s or 'Unclassified', 'count': n}
                                   for s, n in proj_by_stage.items()],
            'top_accounts_by_rfq':[{'account': n, 'rfqs': int(r)}
                                    for n, r in top_accounts],
        },
    )


# ─── Report exports (CSV) ─────────────────────────────────────────────
def _csv_response(headers, rows, filename):
    sio = io.StringIO()
    w = csv.writer(sio)
    w.writerow(headers)
    for r in rows:
        w.writerow(r)
    resp = Response(sio.getvalue(), mimetype='text/csv')
    resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp


@bp.route('/api/presales/reports/accounts.csv', methods=['GET'])
def api_report_accounts():
    if not _require_login():
        return 'login required', 401
    emp = _current_emp()
    rows = _scoped_accounts(emp).all()
    data = []
    for a in rows:
        opp_total = Opportunity.query.filter_by(source_account_id=a.id).count()
        opp_won   = Opportunity.query.filter_by(source_account_id=a.id,
                                                stage='Won').count()
        data.append([
            a.id, a.name, a.industry or '', a.country or '', a.state or '',
            getattr(a, 'dev_stage', '') or '',
            getattr(a, 'pic_emp_code', '') or '',
            'Y' if getattr(a, 'strategic_flag', False) else 'N',
            getattr(a, 'priority', '') or '',
            str(a.last_activity_at)[:16] if getattr(a, 'last_activity_at', None) else '',
            str(a.next_action_at) if getattr(a, 'next_action_at', None) else '',
            opp_total, opp_won,
        ])
    return _csv_response(
        ['ID','Name','Industry','Country','State',
         'Dev Stage','PIC','Strategic','Priority',
         'Last Activity','Next Action Date','RFQs','Won'],
        data, 'account_development_register.csv',
    )


@bp.route('/api/presales/reports/projects.csv', methods=['GET'])
def api_report_projects():
    if not _require_login():
        return 'login required', 401
    emp = _current_emp()
    rows = _scoped_projects(emp).all()
    data = []
    for p in rows:
        opp_total = Opportunity.query.filter_by(source_project_id=p.id).count()
        opp_won   = Opportunity.query.filter_by(source_project_id=p.id,
                                                stage='Won').count()
        data.append([
            p.id, p.project_code or '', p.name, p.project_type or '',
            p.industry or '', p.state or '',
            p.stage or '', p.priority or '',
            p.pic_emp_code or '',
            str(p.announcement_date) if p.announcement_date else '',
            str(p.expected_start_date) if p.expected_start_date else '',
            str(p.last_update_at)[:16] if p.last_update_at else '',
            str(p.next_review_at) if p.next_review_at else '',
            opp_total, opp_won,
        ])
    return _csv_response(
        ['ID','Code','Name','Type','Industry','State',
         'Stage','Priority','PIC',
         'Announcement Date','Expected Start','Last Update','Next Review',
         'RFQs','Won'],
        data, 'project_intelligence_register.csv',
    )


@bp.route('/api/presales/reports/attribution.csv', methods=['GET'])
def api_report_attribution():
    if not _require_login():
        return 'login required', 401
    emp = _current_emp()
    q = db.session.query(
        Opportunity.id, Opportunity.opp_number, Opportunity.title,
        Opportunity.stage, Opportunity.value_inr,
        Opportunity.source_type, Opportunity.source_account_id,
        Opportunity.source_project_id, Opportunity.created_at,
    ).filter(Opportunity.source_type.isnot(None))
    if emp.role != 'admin':
        # limit to opps whose source Account belongs to the user's scope
        scoped_ids = {a.id for a in _scoped_accounts(emp).all()}
        q = q.filter((Opportunity.source_account_id.in_(scoped_ids)) |
                     (Opportunity.source_project_id.isnot(None)))
    rows = q.order_by(Opportunity.created_at.desc()).limit(5000).all()
    # Resolve names
    acc_ids = {r[6] for r in rows if r[6]}
    prj_ids = {r[7] for r in rows if r[7]}
    acc_names = {c.id: c.name for c in Company.query.filter(Company.id.in_(acc_ids)).all()} if acc_ids else {}
    prj_names = {p.id: p.name for p in Project.query.filter(Project.id.in_(prj_ids)).all()} if prj_ids else {}
    data = []
    for r in rows:
        data.append([
            r[0], r[1] or '', r[2] or '', r[3] or '',
            float(r[4] or 0), r[5] or '',
            acc_names.get(r[6], ''), prj_names.get(r[7], ''),
            str(r[8])[:10] if r[8] else '',
        ])
    return _csv_response(
        ['Opp ID','OPP Number','Title','Stage','Value ₹',
         'Source Type','Source Account','Source Project','Created'],
        data, 'rfq_source_attribution.csv',
    )
