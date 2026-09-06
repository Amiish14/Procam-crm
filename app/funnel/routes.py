"""
Funnels — Account Development + Project Intelligence
(Phase 11 of the CRM upgrade, spec §41-46).

Two funnels expose the same shape of output:

    [
      { stage, count, value, avg_duration_days, ageing_bucket,
        conversion_pct_to_next },
      ...
    ]

Account-Development funnel stages
    Target Account, Account Research, Contact Identified,
    Contact Established, Meeting/Engagement, Relationship Development,
    Opportunity Identified, RFQ, Quote, Negotiation, Won, TMS Project

Project-Intelligence funnel stages
    Project Identified, Project Development, EPC Identified,
    Procurement, RFQ Expected, RFQ, Quote, Negotiation, Won, TMS Project

Where the stages come from
    * Target …Relationship Development  -> Company.dev_stage
    * Opportunity Identified / RFQ / Quote / Negotiation / Won
                                         -> Opportunity.stage + RFQ + Quote
    * TMS Project                        -> WonHandover status
    * Project Identified …Procurement    -> Project.stage
    * RFQ Expected                       -> Project.stage
    * RFQ / Quote / Negotiation / Won    -> Opportunities linked to project
                                            via source_project_id
    * TMS Project                        -> WonHandover(project_id=…)

Filter params (spec §44) applied where the underlying entity carries the
column:  pic, lead_driver, rate_sourcing_pic, assignment_role, vertical,
industry, branch, account, customer, project, country, state, service,
stage, source, date_from, date_to, financial_year
"""
from datetime import date, datetime, timedelta
from functools import wraps
from statistics import median

from flask import Blueprint, jsonify, render_template, request, session, redirect, url_for

from app import db


bp = Blueprint('funnel', __name__)


# ─── auth helpers ────────────────────────────────────────────────────────
def _require_auth(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not session.get('emp_code'):
            return jsonify(error='Not authenticated'), 401
        return f(*a, **kw)
    return wrap


def _parse_date(v):
    if not v:
        return None
    try:
        return datetime.strptime(str(v)[:10], '%Y-%m-%d').date()
    except Exception:
        return None


def _fy_bounds(fy):
    """Indian FY 'YYYY-YY' or 'YYYY' → (from_date, to_date)."""
    try:
        yr = int(str(fy)[:4])
        return date(yr, 4, 1), date(yr + 1, 3, 31)
    except Exception:
        return None, None


# ═════════════════════════════════════════════════════════════════════════
# Stage vocabulary
# ═════════════════════════════════════════════════════════════════════════
ACCOUNT_DEV_FUNNEL = (
    'Target Account', 'Account Research', 'Contact Identified',
    'Contact Established', 'Meeting/Engagement',
    'Relationship Development', 'Opportunity Identified',
    'RFQ', 'Quote', 'Negotiation', 'Won', 'TMS Project',
)

PROJECT_INTEL_FUNNEL = (
    'Project Identified', 'Project Development', 'EPC Identified',
    'Procurement', 'RFQ Expected', 'RFQ', 'Quote', 'Negotiation',
    'Won', 'TMS Project',
)

# Map funnel-stage → list of Company.dev_stage values (§ACCOUNT_DEV_STAGES)
_ACCT_DEV_STAGE_MAP = {
    'Target Account':           ['Target Identified'],
    'Account Research':         ['Researching'],
    'Contact Identified':       ['Contact Identification', 'Initial Outreach'],
    'Contact Established':      ['Contact Established'],
    'Meeting/Engagement':       ['Meeting Planned', 'Meeting Completed'],
    'Relationship Development': ['Relationship Development', 'Active Account'],
    'Opportunity Identified':   ['Opportunity Identified'],
}

# Map funnel-stage → list of Project.stage values
_PROJECT_STAGE_MAP = {
    'Project Identified':  ['Project Identified'],
    'Project Development': ['Announced / Proposed', 'Planning',
                             'Approval / Funding'],
    'EPC Identified':      ['EPC Tendering', 'EPC Appointed'],
    'Procurement':         ['Procurement Started', 'Equipment Procurement'],
    'RFQ Expected':        ['Logistics Opportunity Identified',
                             'RFQ Expected'],
}

# Opportunity/RFQ/Quote/handover stage values
_OPP_STAGE_RFQ         = ['RFQ']
_OPP_STAGE_QUOTE       = ['Quote', 'Quotation']
_OPP_STAGE_NEGOTIATION = ['Negotiation']
_OPP_STAGE_WON         = ['Won']


# ═════════════════════════════════════════════════════════════════════════
# Filter application
# ═════════════════════════════════════════════════════════════════════════
def _pick_filters():
    f = {}
    for k in ('pic', 'lead_driver', 'rate_sourcing_pic', 'assignment_role',
              'vertical', 'industry', 'branch', 'account', 'customer',
              'project', 'country', 'state', 'service', 'stage', 'source',
              'financial_year'):
        v = (request.args.get(k) or '').strip()
        if v:
            f[k] = v
    f['date_from'] = _parse_date(request.args.get('date_from') or request.args.get('date'))
    f['date_to'] = _parse_date(request.args.get('date_to'))
    if f.get('financial_year'):
        d1, d2 = _fy_bounds(f['financial_year'])
        if d1 and not f['date_from']:
            f['date_from'] = d1
        if d2 and not f['date_to']:
            f['date_to'] = d2
    return f


def _apply_company_filters(q, f):
    from app import Company
    if f.get('industry'):
        q = q.filter(Company.industry == f['industry'])
    if f.get('country'):
        q = q.filter(Company.country == f['country'])
    if f.get('state'):
        q = q.filter(Company.state == f['state'])
    if f.get('account'):
        q = q.filter(Company.name.ilike(f'%{f["account"]}%'))
    if f.get('pic'):
        # Company has no direct pic — filter via CompanyOwner if available
        try:
            from presales.models import CompanyOwner
            q = q.filter(Company.id.in_(
                db.session.query(CompanyOwner.company_id)
                .filter(CompanyOwner.emp_code == f['pic'])))
        except Exception:
            pass
    return q


def _apply_opp_filters(q, f):
    from app import Opportunity, Company
    if f.get('pic') or f.get('lead_driver'):
        pic = f.get('pic') or f.get('lead_driver')
        q = q.filter(Opportunity.owner_emp_code == pic)
    if f.get('account') or f.get('customer'):
        term = f.get('account') or f.get('customer')
        q = q.join(Company, Opportunity.company_id == Company.id, isouter=True)\
             .filter(Company.name.ilike(f'%{term}%'))
    if f.get('industry'):
        q = q.join(Company, Opportunity.company_id == Company.id, isouter=True)\
             .filter(Company.industry == f['industry'])
    if f.get('date_from'):
        q = q.filter(Opportunity.created_at >= datetime.combine(
            f['date_from'], datetime.min.time()))
    if f.get('date_to'):
        q = q.filter(Opportunity.created_at <= datetime.combine(
            f['date_to'], datetime.max.time()))
    if f.get('source'):
        q = q.filter(Opportunity.source_type == f['source'])
    return q


def _apply_project_filters(q, f):
    from presales.models_projects import Project
    if f.get('pic'):
        q = q.filter(Project.pic_emp_code == f['pic'])
    if f.get('vertical'):
        q = q.filter(Project.procam_vertical == f['vertical'])
    if f.get('industry'):
        q = q.filter(Project.industry == f['industry'])
    if f.get('country'):
        q = q.filter(Project.country == f['country'])
    if f.get('state'):
        q = q.filter(Project.state == f['state'])
    if f.get('branch'):
        q = q.filter(Project.branch == f['branch'])
    if f.get('project'):
        q = q.filter(Project.name.ilike(f'%{f["project"]}%'))
    if f.get('date_from'):
        q = q.filter(Project.created_at >= datetime.combine(
            f['date_from'], datetime.min.time()))
    if f.get('date_to'):
        q = q.filter(Project.created_at <= datetime.combine(
            f['date_to'], datetime.max.time()))
    return q


def _apply_rfq_filters(q, f):
    from app.models.rfq import RFQ
    if f.get('lead_driver') or f.get('pic'):
        pic = f.get('lead_driver') or f.get('pic')
        q = q.filter(RFQ.lead_driver == pic)
    if f.get('date_from'):
        q = q.filter(RFQ.received_date >= f['date_from'])
    if f.get('date_to'):
        q = q.filter(RFQ.received_date <= f['date_to'])
    return q


def _apply_quote_filters(q, f):
    from app.models.quote import Quote
    if f.get('pic'):
        q = q.filter(Quote.prepared_by_id == f['pic'])
    if f.get('date_from'):
        q = q.filter(Quote.quote_date >= f['date_from'])
    if f.get('date_to'):
        q = q.filter(Quote.quote_date <= f['date_to'])
    return q


# ═════════════════════════════════════════════════════════════════════════
# Ageing bucket & duration helpers
# ═════════════════════════════════════════════════════════════════════════
def _ageing_bucket_from_days(days):
    if days is None:
        return 'Unknown'
    if days <= 7:
        return '0-7'
    if days <= 30:
        return '8-30'
    if days <= 90:
        return '31-90'
    return '90+'


def _summarise_days(days_list):
    if not days_list:
        return None, 'Unknown'
    med = median(days_list)
    return round(med, 1), _ageing_bucket_from_days(med)


# ═════════════════════════════════════════════════════════════════════════
# Stage row builders (each returns (count, value, entered_ats, entity_ids))
# ═════════════════════════════════════════════════════════════════════════
def _accounts_by_stage(stage_names, f):
    """Companies whose dev_stage is in stage_names."""
    from app import Company
    q = Company.query.filter(Company.dev_stage.in_(list(stage_names)))
    q = _apply_company_filters(q, f)
    rows = q.all()
    return rows


def _opps_by_stage(stage_names, f):
    from app import Opportunity
    q = Opportunity.query.filter(Opportunity.stage.in_(list(stage_names)))
    q = _apply_opp_filters(q, f)
    return q.all()


def _rfqs_by_status(status_names, f):
    from app.models.rfq import RFQ
    q = RFQ.query.filter(RFQ.status.in_(list(status_names)))
    q = _apply_rfq_filters(q, f)
    return q.all()


def _quotes_by_status(status_names, f):
    from app.models.quote import Quote
    q = Quote.query.filter(Quote.status.in_(list(status_names)))
    q = _apply_quote_filters(q, f)
    return q.all()


def _projects_by_stage(stage_names, f):
    from presales.models_projects import Project
    q = Project.query.filter(Project.stage.in_(list(stage_names)))
    q = _apply_project_filters(q, f)
    return q.all()


def _handovers(f):
    from app.models.tms_handover import WonHandover
    q = WonHandover.query
    if f.get('date_from'):
        q = q.filter(WonHandover.created_at >= datetime.combine(
            f['date_from'], datetime.min.time()))
    if f.get('date_to'):
        q = q.filter(WonHandover.created_at <= datetime.combine(
            f['date_to'], datetime.max.time()))
    return q.all()


# ═════════════════════════════════════════════════════════════════════════
# Duration + conversion
# ═════════════════════════════════════════════════════════════════════════
def _days_in_stage_account(company):
    """Time since Company entered its current dev_stage."""
    try:
        from presales.models import AccountStageHistory
        row = (AccountStageHistory.query
               .filter_by(account_id=company.id,
                          to_stage=company.dev_stage)
               .order_by(AccountStageHistory.changed_at.desc()).first())
        if row and row.changed_at:
            return (datetime.utcnow() - row.changed_at).days
    except Exception:
        pass
    if getattr(company, 'created_at', None):
        return (datetime.utcnow() - company.created_at).days
    return None


def _days_in_stage_opp(opp):
    if getattr(opp, 'updated_at', None):
        return (datetime.utcnow() - opp.updated_at).days
    return None


def _days_in_stage_project(project):
    try:
        from presales.models_projects import ProjectStageHistory
        row = (ProjectStageHistory.query
               .filter_by(project_id=project.id, to_stage=project.stage)
               .order_by(ProjectStageHistory.changed_at.desc()).first())
        if row and row.changed_at:
            return (datetime.utcnow() - row.changed_at).days
    except Exception:
        pass
    if getattr(project, 'created_at', None):
        return (datetime.utcnow() - project.created_at).days
    return None


def _account_conv_pct(stage_from, stage_to, months=6):
    """% of accounts whose most recent transition FROM stage_from went
    TO stage_to within the last N months."""
    try:
        from presales.models import AccountStageHistory
        since = datetime.utcnow() - timedelta(days=months * 30)
        # For every account that left `stage_from` since `since`, did it
        # reach `stage_to`?
        left_from = (db.session.query(AccountStageHistory.account_id)
                     .filter(AccountStageHistory.from_stage == stage_from,
                             AccountStageHistory.changed_at >= since)
                     .distinct().all())
        left_ids = [r[0] for r in left_from]
        if not left_ids:
            return None
        to_next = (db.session.query(AccountStageHistory.account_id)
                   .filter(AccountStageHistory.to_stage == stage_to,
                           AccountStageHistory.changed_at >= since,
                           AccountStageHistory.account_id.in_(left_ids))
                   .distinct().count())
        return round(100.0 * to_next / len(left_ids), 1)
    except Exception:
        return None


# ═════════════════════════════════════════════════════════════════════════
# Funnel builders
# ═════════════════════════════════════════════════════════════════════════
def _row(stage, count, value, days_list, conversion=None):
    med, bucket = _summarise_days(days_list)
    return {
        'stage':                   stage,
        'count':                   int(count or 0),
        'value':                   float(value or 0),
        'avg_duration_days':       med,
        'ageing_bucket':           bucket,
        'conversion_pct_to_next':  conversion,
    }


def _account_development_funnel(f):
    stages_out = []

    # Early stages (dev_stage on Company)
    for i, funnel_stage in enumerate(
            ['Target Account', 'Account Research', 'Contact Identified',
             'Contact Established', 'Meeting/Engagement',
             'Relationship Development', 'Opportunity Identified']):
        raw_stages = _ACCT_DEV_STAGE_MAP.get(funnel_stage, [])
        rows = _accounts_by_stage(raw_stages, f) if raw_stages else []
        days = [d for d in (_days_in_stage_account(c) for c in rows)
                if d is not None]
        # conversion — approximate: from first raw stage of THIS funnel-stage
        # to first raw stage of NEXT funnel-stage
        conv = None
        try:
            next_stage_name = ACCOUNT_DEV_FUNNEL[
                ACCOUNT_DEV_FUNNEL.index(funnel_stage) + 1]
            next_raw = _ACCT_DEV_STAGE_MAP.get(next_stage_name, [])
            if raw_stages and next_raw:
                conv = _account_conv_pct(raw_stages[0], next_raw[0])
        except (IndexError, ValueError):
            conv = None
        stages_out.append(_row(funnel_stage, len(rows), 0, days, conv))

    # RFQ — from RFQ table
    rfqs = _rfqs_by_status(['Received', 'Rate Sourcing', 'Quote Preparation'], f)
    days = [( (datetime.utcnow().date() - r.received_date).days
              if r.received_date else None) for r in rfqs]
    days = [d for d in days if d is not None]
    stages_out.append(_row('RFQ', len(rfqs),
                           0, days))

    # Quote — quotes in Draft/Awaiting/Approved/Submitted
    quotes = _quotes_by_status(
        ['Draft', 'Awaiting Approval', 'Approved', 'Submitted'], f)
    qval = sum(float(q.total_amount or 0) for q in quotes)
    qdays = [((datetime.utcnow() - q.updated_at).days
              if q.updated_at else None) for q in quotes]
    qdays = [d for d in qdays if d is not None]
    stages_out.append(_row('Quote', len(quotes), qval, qdays))

    # Negotiation
    neg_q = _quotes_by_status(['Under Negotiation'], f)
    neg_o = _opps_by_stage(['Negotiation'], f)
    nval = (sum(float(q.total_amount or 0) for q in neg_q) +
            sum(float(o.value_inr or 0) for o in neg_o))
    ndays = ([(datetime.utcnow() - q.updated_at).days for q in neg_q
              if q.updated_at] +
             [_days_in_stage_opp(o) for o in neg_o if _days_in_stage_opp(o) is not None])
    stages_out.append(_row('Negotiation', len(neg_q) + len(neg_o),
                           nval, ndays))

    # Won
    won_q = _quotes_by_status(['Won'], f)
    won_o = _opps_by_stage(['Won'], f)
    wval = (sum(float(q.total_amount or 0) for q in won_q) +
            sum(float(o.value_inr or 0) for o in won_o))
    wdays = ([(datetime.utcnow() - q.won_at).days for q in won_q if q.won_at] +
             [_days_in_stage_opp(o) for o in won_o if _days_in_stage_opp(o) is not None])
    stages_out.append(_row('Won', len(won_q) + len(won_o), wval, wdays))

    # TMS Project
    handovers = _handovers(f)
    hval = sum(float(h.won_value or 0) for h in handovers)
    hdays = [((datetime.utcnow() - h.created_at).days
              if h.created_at else None) for h in handovers]
    hdays = [d for d in hdays if d is not None]
    stages_out.append(_row('TMS Project', len(handovers), hval, hdays))

    return stages_out


def _project_intelligence_funnel(f):
    stages_out = []

    for funnel_stage in ('Project Identified', 'Project Development',
                         'EPC Identified', 'Procurement', 'RFQ Expected'):
        raw = _PROJECT_STAGE_MAP.get(funnel_stage, [])
        rows = _projects_by_stage(raw, f) if raw else []
        pvalue = sum(float(p.estimated_value_inr or 0) for p in rows)
        days = [d for d in (_days_in_stage_project(p) for p in rows)
                if d is not None]
        stages_out.append(_row(funnel_stage, len(rows), pvalue, days))

    # RFQ — RFQs whose project_id links to a Project
    from app.models.rfq import RFQ
    rq = _apply_rfq_filters(
        RFQ.query.filter(RFQ.project_id.isnot(None),
                         RFQ.status.in_(['Received', 'Rate Sourcing',
                                         'Quote Preparation'])), f)
    rfq_rows = rq.all()
    rdays = [((datetime.utcnow().date() - r.received_date).days
              if r.received_date else None) for r in rfq_rows]
    rdays = [d for d in rdays if d is not None]
    stages_out.append(_row('RFQ', len(rfq_rows), 0, rdays))

    # Quote — quotes whose rfq.project_id is set
    from app.models.quote import Quote
    qq = _apply_quote_filters(
        Quote.query.filter(Quote.rfq_id.in_(
            db.session.query(RFQ.id).filter(RFQ.project_id.isnot(None))))
        .filter(Quote.status.in_(['Draft', 'Awaiting Approval', 'Approved',
                                   'Submitted'])), f)
    q_rows = qq.all()
    qval = sum(float(q.total_amount or 0) for q in q_rows)
    qdays = [((datetime.utcnow() - q.updated_at).days
              if q.updated_at else None) for q in q_rows]
    qdays = [d for d in qdays if d is not None]
    stages_out.append(_row('Quote', len(q_rows), qval, qdays))

    # Negotiation
    from app import Opportunity
    neg_q = _apply_quote_filters(
        Quote.query.filter(Quote.status == 'Under Negotiation',
                           Quote.rfq_id.in_(
                               db.session.query(RFQ.id).filter(
                                   RFQ.project_id.isnot(None))))
        , f).all()
    neg_o = _apply_opp_filters(
        Opportunity.query.filter(Opportunity.stage == 'Negotiation',
                                 Opportunity.source_project_id.isnot(None)),
        f).all()
    nval = (sum(float(q.total_amount or 0) for q in neg_q) +
            sum(float(o.value_inr or 0) for o in neg_o))
    ndays = ([(datetime.utcnow() - q.updated_at).days for q in neg_q
              if q.updated_at] +
             [_days_in_stage_opp(o) for o in neg_o if _days_in_stage_opp(o) is not None])
    stages_out.append(_row('Negotiation', len(neg_q) + len(neg_o), nval, ndays))

    # Won
    won_q = _apply_quote_filters(
        Quote.query.filter(Quote.status == 'Won',
                           Quote.rfq_id.in_(
                               db.session.query(RFQ.id).filter(
                                   RFQ.project_id.isnot(None))))
        , f).all()
    won_o = _apply_opp_filters(
        Opportunity.query.filter(Opportunity.stage == 'Won',
                                 Opportunity.source_project_id.isnot(None)),
        f).all()
    wval = (sum(float(q.total_amount or 0) for q in won_q) +
            sum(float(o.value_inr or 0) for o in won_o))
    wdays = ([(datetime.utcnow() - q.won_at).days for q in won_q if q.won_at] +
             [_days_in_stage_opp(o) for o in won_o if _days_in_stage_opp(o) is not None])
    stages_out.append(_row('Won', len(won_q) + len(won_o), wval, wdays))

    # TMS Project — handover rows tied to a project
    from app.models.tms_handover import WonHandover
    handovers = WonHandover.query.filter(WonHandover.project_id.isnot(None)).all()
    hval = sum(float(h.won_value or 0) for h in handovers)
    hdays = [((datetime.utcnow() - h.created_at).days
              if h.created_at else None) for h in handovers]
    hdays = [d for d in hdays if d is not None]
    stages_out.append(_row('TMS Project', len(handovers), hval, hdays))

    return stages_out


# ═════════════════════════════════════════════════════════════════════════
# Routes
# ═════════════════════════════════════════════════════════════════════════
@bp.route('/api/funnel/account-development', methods=['GET'])
@_require_auth
def api_funnel_account_development():
    f = _pick_filters()
    return jsonify(stages=_account_development_funnel(f))


@bp.route('/api/funnel/project-intelligence', methods=['GET'])
@_require_auth
def api_funnel_project_intelligence():
    f = _pick_filters()
    return jsonify(stages=_project_intelligence_funnel(f))


@bp.route('/api/funnel/count-vs-value', methods=['GET'])
@_require_auth
def api_funnel_count_vs_value():
    f = _pick_filters()
    which = (request.args.get('funnel') or 'account-development').lower()
    if which == 'project-intelligence':
        stages = _project_intelligence_funnel(f)
    else:
        stages = _account_development_funnel(f)
    return jsonify(
        funnel=which,
        by_count={s['stage']: s['count'] for s in stages},
        by_value={s['stage']: s['value'] for s in stages},
    )


# ─── HTML views ──────────────────────────────────────────────────────────
@bp.route('/funnels/account-development')
def account_dev_page():
    if not session.get('emp_code'):
        return redirect(url_for('login'))
    return render_template('funnel/account_development.html')


@bp.route('/funnels/project-intelligence')
def project_intel_page():
    if not session.get('emp_code'):
        return redirect(url_for('login'))
    return render_template('funnel/project_intelligence.html')
