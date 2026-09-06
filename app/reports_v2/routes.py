"""Reports v2 — routes (Phase 15).

A hub of read-only cross-cutting reports that satisfy spec §59-62.  Every
report:

  * Renders through the same ``templates/reports_v2/_generic.html``
    template driven by a small ``report_meta`` dict of column definitions.
  * Accepts ``?format=xlsx`` to stream an ``openpyxl`` workbook.
  * Applies simple query-string filters (owner, date range, priority,
    stage, competitor, vertical, ...).
  * Is safe to hit even when the underlying facts are empty — the
    template falls back to an empty-state banner.

All queries are read-only.  This blueprint never mutates.  Add-only
across the existing routes surface.
"""
from datetime import date, datetime, timedelta
from functools import wraps
from io import BytesIO

from flask import (Blueprint, jsonify, render_template, request, session,
                   send_file, abort, url_for)
from sqlalchemy import or_, and_, func

from app import db


bp = Blueprint('reports_v2', __name__)


# ─── auth helper ─────────────────────────────────────────────────────────
def _require_auth(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not session.get('emp_code'):
            return ('', 302, {'Location': '/login'})
        return f(*a, **kw)
    return wrap


def _emp():
    return session.get('emp_code') or ''


def _parse_date(v):
    if not v:
        return None
    try:
        return datetime.strptime(v[:10], '%Y-%m-%d').date()
    except Exception:
        return None


# ─── Excel helper ────────────────────────────────────────────────────────
def _stream_xlsx(rows, columns, slug, filters=None):
    """Serialise `rows` (list of dicts) into an .xlsx and return a Flask
    response streaming the workbook."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
    except Exception:                                                # pragma: no cover
        return jsonify(error='openpyxl not installed'), 500
    wb = Workbook()
    ws = wb.active
    ws.title = slug[:31] or 'Report'

    # Header with filter summary as a first row.
    if filters:
        summary = 'Filters: ' + '  |  '.join(
            f'{k}={v}' for k, v in filters.items() if v not in (None, ''))
        if summary.strip() != 'Filters:':
            ws.append([summary])
            ws.merge_cells(start_row=1, start_column=1,
                           end_row=1, end_column=max(1, len(columns)))
            ws.cell(row=1, column=1).font = Font(italic=True, color='666666')
            ws.append([])
    headers = [c['label'] for c in columns]
    ws.append(headers)
    hdr_row = ws.max_row
    for c_ix in range(1, len(headers) + 1):
        cell = ws.cell(row=hdr_row, column=c_ix)
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='C72435')
        cell.alignment = Alignment(vertical='center')

    for row in rows:
        ws.append([row.get(c['key'], '') for c in columns])

    for i, c in enumerate(columns, start=1):
        ws.column_dimensions[chr(64 + i) if i <= 26 else 'A'].width = \
            max(12, min(40, len(c['label']) + 4))

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    return send_file(
        buf,
        as_attachment=True,
        download_name=f'{slug}_{ts}.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.'
                 'spreadsheetml.sheet',
    )


def _serve(rows, meta, filters):
    """Render either HTML or XLSX depending on ?format="""
    fmt = (request.args.get('format') or 'html').lower()
    if fmt == 'xlsx':
        return _stream_xlsx(rows, meta['columns'], meta['slug'],
                            filters=filters)
    return render_template('reports_v2/_generic.html',
                           rows=rows, meta=meta, filters=filters)


# =========================================================================
# HUB
# =========================================================================
@bp.route('/reports')
def hub():
    if not session.get('emp_code'):
        return ('', 302, {'Location': '/login'})
    return render_template('reports_v2/index.html', reports=REPORT_INDEX)


# =========================================================================
# Action Management (spec §59)
# =========================================================================
def _task_rows(query):
    from app.models.task_engine import TaskInstance
    rows = []
    for t in query.all():
        rows.append({
            'id':           t.id,
            'task_key':     t.task_key,
            'entity':       f'{t.entity_type}#{t.entity_id}',
            'entity_display': t.entity_display or '',
            'owner_user':   t.owner_user_id or '',
            'owner_role':   t.owner_role or '',
            'status':       t.status,
            'priority':     t.priority,
            'created_at':   str(t.created_at)[:16] if t.created_at else '',
            'due_at':       str(t.due_at)[:16] if t.due_at else '',
            'completed_at': str(t.completed_at)[:16] if t.completed_at else '',
            'overdue':      'Yes' if t.is_overdue else 'No',
        })
    return rows


_TASK_COLS = [
    {'key': 'id', 'label': 'ID'},
    {'key': 'task_key', 'label': 'Task'},
    {'key': 'entity', 'label': 'Entity'},
    {'key': 'entity_display', 'label': 'Entity Ref'},
    {'key': 'owner_user', 'label': 'Owner'},
    {'key': 'owner_role', 'label': 'Owner Role'},
    {'key': 'status', 'label': 'Status'},
    {'key': 'priority', 'label': 'Priority'},
    {'key': 'created_at', 'label': 'Created'},
    {'key': 'due_at', 'label': 'Due'},
    {'key': 'completed_at', 'label': 'Completed'},
    {'key': 'overdue', 'label': 'Overdue?'},
]


def _apply_task_filters(q, TaskInstance):
    owner = (request.args.get('owner') or '').strip()
    status = (request.args.get('status') or '').strip()
    priority = (request.args.get('priority') or '').strip()
    from_d = _parse_date(request.args.get('from'))
    to_d = _parse_date(request.args.get('to'))
    if owner:
        q = q.filter(TaskInstance.owner_user_id == owner)
    if status:
        q = q.filter(TaskInstance.status == status)
    if priority:
        try:
            q = q.filter(TaskInstance.priority == int(priority))
        except Exception:
            pass
    if from_d:
        q = q.filter(TaskInstance.created_at >= datetime.combine(
            from_d, datetime.min.time()))
    if to_d:
        q = q.filter(TaskInstance.created_at <= datetime.combine(
            to_d, datetime.max.time()))
    return q


@bp.route('/reports/open-tasks')
@_require_auth
def rep_open_tasks():
    from app.models.task_engine import TaskInstance, TaskInstanceStatus
    q = TaskInstance.query.filter(
        TaskInstance.status.in_(TaskInstanceStatus.OPEN))
    q = _apply_task_filters(q, TaskInstance)
    q = q.order_by(TaskInstance.due_at.asc().nullslast())
    return _serve(_task_rows(q), {
        'slug': 'open_tasks',
        'title': 'Open Tasks',
        'category': 'Action Management',
        'columns': _TASK_COLS,
    }, dict(request.args))


@bp.route('/reports/overdue-tasks')
@_require_auth
def rep_overdue_tasks():
    from app.models.task_engine import TaskInstance, TaskInstanceStatus
    now = datetime.utcnow()
    q = TaskInstance.query.filter(
        TaskInstance.status.in_(TaskInstanceStatus.OPEN),
        TaskInstance.due_at.isnot(None),
        TaskInstance.due_at < now,
    )
    q = _apply_task_filters(q, TaskInstance)
    q = q.order_by(TaskInstance.due_at.asc())
    return _serve(_task_rows(q), {
        'slug': 'overdue_tasks',
        'title': 'Overdue Tasks',
        'category': 'Action Management',
        'columns': _TASK_COLS,
    }, dict(request.args))


@bp.route('/reports/tasks-by-user')
@_require_auth
def rep_tasks_by_user():
    from app.models.task_engine import TaskInstance
    q = (db.session.query(
            TaskInstance.owner_user_id.label('owner'),
            TaskInstance.status.label('status'),
            func.count(TaskInstance.id).label('n'),
         )
         .group_by(TaskInstance.owner_user_id, TaskInstance.status)
         .order_by(TaskInstance.owner_user_id))
    rows = [{'owner_user': r.owner or '',
             'status': r.status, 'count': int(r.n)} for r in q.all()]
    return _serve(rows, {
        'slug': 'tasks_by_user',
        'title': 'Tasks by User',
        'category': 'Action Management',
        'columns': [
            {'key': 'owner_user', 'label': 'Owner'},
            {'key': 'status', 'label': 'Status'},
            {'key': 'count', 'label': 'Count'},
        ],
    }, dict(request.args))


@bp.route('/reports/tasks-by-role')
@_require_auth
def rep_tasks_by_role():
    from app.models.task_engine import TaskInstance
    q = (db.session.query(
            TaskInstance.owner_role.label('role'),
            TaskInstance.status.label('status'),
            func.count(TaskInstance.id).label('n'),
         )
         .group_by(TaskInstance.owner_role, TaskInstance.status)
         .order_by(TaskInstance.owner_role))
    rows = [{'role': r.role or '', 'status': r.status,
             'count': int(r.n)} for r in q.all()]
    return _serve(rows, {
        'slug': 'tasks_by_role',
        'title': 'Tasks by Role',
        'category': 'Action Management',
        'columns': [
            {'key': 'role', 'label': 'Role'},
            {'key': 'status', 'label': 'Status'},
            {'key': 'count', 'label': 'Count'},
        ],
    }, dict(request.args))


@bp.route('/reports/tasks-by-vertical')
@_require_auth
def rep_tasks_by_vertical():
    # Vertical isn't modelled on TaskInstance; approximate via task_key
    # prefix (module) as a stand-in for now.
    from app.models.task_engine import TaskInstance
    q = (db.session.query(
            func.substr(TaskInstance.task_key, 1, 20).label('bucket'),
            func.count(TaskInstance.id).label('n'),
         )
         .group_by('bucket')
         .order_by(func.count(TaskInstance.id).desc()))
    rows = [{'bucket': r.bucket or '', 'count': int(r.n)}
            for r in q.all()]
    return _serve(rows, {
        'slug': 'tasks_by_vertical',
        'title': 'Tasks by Module / Vertical',
        'category': 'Action Management',
        'columns': [
            {'key': 'bucket', 'label': 'Module'},
            {'key': 'count', 'label': 'Count'},
        ],
    }, dict(request.args))


@bp.route('/reports/task-completion')
@_require_auth
def rep_task_completion():
    from app.models.task_engine import TaskInstance, TaskInstanceStatus
    q = TaskInstance.query.filter(
        TaskInstance.status == TaskInstanceStatus.COMPLETED,
        TaskInstance.completed_at.isnot(None),
    )
    from_d = _parse_date(request.args.get('from'))
    to_d   = _parse_date(request.args.get('to'))
    if from_d:
        q = q.filter(TaskInstance.completed_at >= datetime.combine(
            from_d, datetime.min.time()))
    if to_d:
        q = q.filter(TaskInstance.completed_at <= datetime.combine(
            to_d, datetime.max.time()))
    buckets = {}
    for t in q.all():
        d = t.completed_at.date().isoformat()
        buckets[d] = buckets.get(d, 0) + 1
    rows = [{'date': d, 'completed': n}
            for d, n in sorted(buckets.items())]
    return _serve(rows, {
        'slug': 'task_completion',
        'title': 'Task Completion Throughput',
        'category': 'Action Management',
        'columns': [
            {'key': 'date', 'label': 'Date'},
            {'key': 'completed', 'label': 'Completed'},
        ],
    }, dict(request.args))


def _sla_report(module_prefix, slug, title):
    """Generic SLA-performance report per module."""
    from app.models.task_engine import (TaskInstance, TaskInstanceStatus,
                                        TaskDefinition)

    # Build a task_key → sla_hours lookup so we can rate SLA hit.
    defs = {d.task_key: d.sla_hours for d in TaskDefinition.query.all()}
    q = TaskInstance.query.filter(
        TaskInstance.task_key.ilike(f'{module_prefix}%'),
        TaskInstance.status == TaskInstanceStatus.COMPLETED,
    )
    rows = []
    total = hit = 0
    for t in q.all():
        sla_h = defs.get(t.task_key)
        if not t.created_at or not t.completed_at or not sla_h:
            continue
        total += 1
        elapsed_h = (t.completed_at - t.created_at).total_seconds() / 3600.0
        met = elapsed_h <= sla_h
        if met:
            hit += 1
        rows.append({
            'id': t.id,
            'task_key': t.task_key,
            'sla_hours': sla_h,
            'elapsed_hours': round(elapsed_h, 2),
            'met_sla': 'Yes' if met else 'No',
            'completed_at': str(t.completed_at)[:16],
        })
    header = [{
        'id': '—', 'task_key': f'{module_prefix}* SUMMARY',
        'sla_hours': '', 'elapsed_hours': '',
        'met_sla': f'{(hit / total * 100.0):.1f}%' if total else '0.0%',
        'completed_at': f'{hit}/{total} hits',
    }]
    return _serve(header + rows, {
        'slug': slug, 'title': title,
        'category': 'Action Management — SLA',
        'columns': [
            {'key': 'id', 'label': 'Task ID'},
            {'key': 'task_key', 'label': 'Task'},
            {'key': 'sla_hours', 'label': 'SLA (h)'},
            {'key': 'elapsed_hours', 'label': 'Elapsed (h)'},
            {'key': 'met_sla', 'label': 'Met SLA?'},
            {'key': 'completed_at', 'label': 'Completed'},
        ],
    }, dict(request.args))


@bp.route('/reports/sla-performance')
@_require_auth
def rep_sla_performance():
    return _sla_report('', 'sla_performance', 'SLA Performance (all)')


@bp.route('/reports/sla-rate-sourcing')
@_require_auth
def rep_sla_rate_sourcing():
    return _sla_report('rate_sourcing', 'sla_rate_sourcing',
                       'SLA — Rate Sourcing')


@bp.route('/reports/sla-quote-prep')
@_require_auth
def rep_sla_quote_prep():
    return _sla_report('quote.prep', 'sla_quote_prep',
                       'SLA — Quote Preparation')


@bp.route('/reports/sla-quote-submit')
@_require_auth
def rep_sla_quote_submit():
    return _sla_report('quote.submit', 'sla_quote_submit',
                       'SLA — Quote Submission')


@bp.route('/reports/sla-negotiation-followup')
@_require_auth
def rep_sla_negotiation_followup():
    return _sla_report('negotiation', 'sla_negotiation_followup',
                       'SLA — Negotiation Follow-up')


@bp.route('/reports/sla-won-handover')
@_require_auth
def rep_sla_won_handover():
    return _sla_report('handover', 'sla_won_handover',
                       'SLA — Won → Handover')


@bp.route('/reports/account-followup-due')
@_require_auth
def rep_account_followup_due():
    from app import Company
    today = date.today()
    q = Company.query.filter(
        Company.is_active.is_(True),
        Company.next_action_at.isnot(None),
        Company.next_action_at <= today,
    ).order_by(Company.next_action_at.asc())
    rows = [{
        'id': c.id, 'name': c.name,
        'pic': c.pic_emp_code or '',
        'priority': c.priority or '',
        'next_action_at': str(c.next_action_at),
        'last_activity_at': str(c.last_activity_at)[:16]
            if c.last_activity_at else '',
    } for c in q.all()]
    return _serve(rows, {
        'slug': 'account_followup_due',
        'title': 'Accounts — Follow-up Due',
        'category': 'Action Management',
        'columns': [
            {'key': 'id', 'label': 'ID'},
            {'key': 'name', 'label': 'Account'},
            {'key': 'pic', 'label': 'PIC'},
            {'key': 'priority', 'label': 'Priority'},
            {'key': 'next_action_at', 'label': 'Next Action'},
            {'key': 'last_activity_at', 'label': 'Last Activity'},
        ],
    }, dict(request.args))


@bp.route('/reports/project-review-due')
@_require_auth
def rep_project_review_due():
    from app.models.task_engine import TaskInstance, TaskInstanceStatus
    q = TaskInstance.query.filter(
        TaskInstance.task_key.ilike('project.review%'),
        TaskInstance.status.in_(TaskInstanceStatus.OPEN),
    )
    return _serve(_task_rows(q), {
        'slug': 'project_review_due',
        'title': 'Project Reviews Due',
        'category': 'Action Management',
        'columns': _TASK_COLS,
    }, dict(request.args))


# =========================================================================
# Competitor (spec §60)
# =========================================================================
@bp.route('/reports/competitor-register')
@_require_auth
def rep_competitor_register():
    from app.models.competitor import CompetitorMaster
    q = CompetitorMaster.query.order_by(CompetitorMaster.name).all()
    rows = [{
        'id': c.id, 'name': c.name,
        'website': c.website or '',
        'country': c.country or '',
        'services': ', '.join(c.services or []),
        'verticals': ', '.join(c.verticals or []),
    } for c in q]
    return _serve(rows, {
        'slug': 'competitor_register',
        'title': 'Competitor Register',
        'category': 'Competitor',
        'columns': [
            {'key': 'id', 'label': 'ID'},
            {'key': 'name', 'label': 'Competitor'},
            {'key': 'website', 'label': 'Website'},
            {'key': 'country', 'label': 'Country'},
            {'key': 'services', 'label': 'Services'},
            {'key': 'verticals', 'label': 'Verticals'},
        ],
    }, dict(request.args))


@bp.route('/reports/competitor-contacts')
@_require_auth
def rep_competitor_contacts():
    from app.models.competitor import CompetitorContact, CompetitorMaster
    rows = []
    for cc in CompetitorContact.query.all():
        cm = CompetitorMaster.query.get(cc.competitor_id) if getattr(
            cc, 'competitor_id', None) else None
        rows.append({
            'id': cc.id,
            'competitor': cm.name if cm else '',
            'name': getattr(cc, 'name', '') or '',
            'designation': getattr(cc, 'designation', '') or '',
            'email': getattr(cc, 'email', '') or '',
            'phone': getattr(cc, 'phone', '') or '',
            'linkedin': getattr(cc, 'linkedin_url', '') or '',
        })
    return _serve(rows, {
        'slug': 'competitor_contacts',
        'title': 'Competitor Contacts',
        'category': 'Competitor',
        'columns': [
            {'key': 'id', 'label': 'ID'},
            {'key': 'competitor', 'label': 'Competitor'},
            {'key': 'name', 'label': 'Name'},
            {'key': 'designation', 'label': 'Designation'},
            {'key': 'email', 'label': 'Email'},
            {'key': 'phone', 'label': 'Phone'},
            {'key': 'linkedin', 'label': 'LinkedIn'},
        ],
    }, dict(request.args))


@bp.route('/reports/competitor-intelligence-log')
@_require_auth
def rep_competitor_intel_log():
    from app.models.competitor import CompetitorIntelligence, CompetitorMaster
    rows = []
    for i in (CompetitorIntelligence.query
              .order_by(CompetitorIntelligence.id.desc()).all()):
        cm = CompetitorMaster.query.get(i.competitor_id) \
            if getattr(i, 'competitor_id', None) else None
        rows.append({
            'id': i.id,
            'competitor': cm.name if cm else '',
            'date': str(getattr(i, 'event_date', '') or '')[:10],
            'source': getattr(i, 'source', '') or '',
            'summary': getattr(i, 'summary', '') or '',
            'url': getattr(i, 'url', '') or '',
        })
    return _serve(rows, {
        'slug': 'competitor_intel_log',
        'title': 'Competitor Intelligence Log',
        'category': 'Competitor',
        'columns': [
            {'key': 'id', 'label': 'ID'},
            {'key': 'competitor', 'label': 'Competitor'},
            {'key': 'date', 'label': 'Date'},
            {'key': 'source', 'label': 'Source'},
            {'key': 'summary', 'label': 'Summary'},
            {'key': 'url', 'label': 'URL'},
        ],
    }, dict(request.args))


@bp.route('/reports/competitor-encounters')
@_require_auth
def rep_competitor_encounters():
    from app.models.competitor import OpportunityCompetitor, CompetitorMaster
    rows = []
    for oc in OpportunityCompetitor.query.all():
        cm = CompetitorMaster.query.get(oc.competitor_id) \
            if getattr(oc, 'competitor_id', None) else None
        rows.append({
            'id': oc.id,
            'competitor': cm.name if cm else '',
            'opportunity_id': getattr(oc, 'opportunity_id', '') or '',
            'lead_id': getattr(oc, 'lead_id', '') or '',
            'status': getattr(oc, 'status', '') or '',
            'competitor_price': getattr(oc, 'competitor_price', '') or '',
            'created_at': str(getattr(oc, 'created_at', '') or '')[:16],
        })
    return _serve(rows, {
        'slug': 'competitor_encounters',
        'title': 'Competitor Encounters',
        'category': 'Competitor',
        'columns': [
            {'key': 'id', 'label': 'ID'},
            {'key': 'competitor', 'label': 'Competitor'},
            {'key': 'opportunity_id', 'label': 'Opp'},
            {'key': 'lead_id', 'label': 'Lead'},
            {'key': 'status', 'label': 'Status'},
            {'key': 'competitor_price', 'label': 'Price'},
            {'key': 'created_at', 'label': 'Created'},
        ],
    }, dict(request.args))


@bp.route('/reports/win-loss-by-competitor')
@_require_auth
def rep_win_loss_by_competitor():
    from app.models.competitor import OpportunityCompetitor, CompetitorMaster
    tally = {}
    for oc in OpportunityCompetitor.query.all():
        cm = CompetitorMaster.query.get(oc.competitor_id) \
            if getattr(oc, 'competitor_id', None) else None
        name = cm.name if cm else '(unknown)'
        st = (getattr(oc, 'status', '') or '').strip()
        t = tally.setdefault(name, {'won': 0, 'lost': 0, 'other': 0})
        if st == 'Winning':
            t['won'] += 1
        elif st == 'Lost To':
            t['lost'] += 1
        else:
            t['other'] += 1
    rows = [{'competitor': n, **v} for n, v in sorted(tally.items())]
    return _serve(rows, {
        'slug': 'win_loss_by_competitor',
        'title': 'Win/Loss by Competitor',
        'category': 'Competitor',
        'columns': [
            {'key': 'competitor', 'label': 'Competitor'},
            {'key': 'won', 'label': 'Winning'},
            {'key': 'lost', 'label': 'Lost To'},
            {'key': 'other', 'label': 'Other'},
        ],
    }, dict(request.args))


@bp.route('/reports/price-comparison')
@_require_auth
def rep_price_comparison():
    from app.models.competitor import OpportunityCompetitor, CompetitorMaster
    rows = []
    for oc in OpportunityCompetitor.query.all():
        cm = CompetitorMaster.query.get(oc.competitor_id) \
            if getattr(oc, 'competitor_id', None) else None
        rows.append({
            'id': oc.id,
            'competitor': cm.name if cm else '',
            'opportunity_id': getattr(oc, 'opportunity_id', '') or '',
            'competitor_price': getattr(oc, 'competitor_price', '') or '',
            'our_price': getattr(oc, 'our_price', '') or '',
            'gap_pct': getattr(oc, 'gap_pct', '') or '',
            'status': getattr(oc, 'status', '') or '',
        })
    return _serve(rows, {
        'slug': 'price_comparison',
        'title': 'Price Comparison',
        'category': 'Competitor',
        'columns': [
            {'key': 'id', 'label': 'ID'},
            {'key': 'competitor', 'label': 'Competitor'},
            {'key': 'opportunity_id', 'label': 'Opp'},
            {'key': 'competitor_price', 'label': 'Comp Price'},
            {'key': 'our_price', 'label': 'Our Price'},
            {'key': 'gap_pct', 'label': 'Gap %'},
            {'key': 'status', 'label': 'Status'},
        ],
    }, dict(request.args))


def _competitor_grouping(dimension_key, label, extractor):
    """Group competitor encounters by a competitor-master attribute."""
    from app.models.competitor import OpportunityCompetitor, CompetitorMaster
    tally = {}
    for oc in OpportunityCompetitor.query.all():
        cm = CompetitorMaster.query.get(oc.competitor_id) \
            if getattr(oc, 'competitor_id', None) else None
        if not cm:
            continue
        value = extractor(cm)
        if isinstance(value, (list, tuple)):
            for v in value:
                v = (v or '').strip() or '(unspecified)'
                tally[v] = tally.get(v, 0) + 1
        else:
            v = (value or '').strip() or '(unspecified)'
            tally[v] = tally.get(v, 0) + 1
    rows = [{dimension_key: k, 'encounters': v}
            for k, v in sorted(tally.items(), key=lambda kv: -kv[1])]
    return _serve(rows, {
        'slug': f'competitor_by_{dimension_key}',
        'title': f'Competitor Encounters by {label}',
        'category': 'Competitor',
        'columns': [
            {'key': dimension_key, 'label': label},
            {'key': 'encounters', 'label': 'Encounters'},
        ],
    }, dict(request.args))


@bp.route('/reports/competitor-by-customer')
@_require_auth
def rep_competitor_by_customer():
    # No direct customer column on OpportunityCompetitor — cross-tabulate
    # by opportunity's linked company.
    from app.models.competitor import OpportunityCompetitor, CompetitorMaster
    from app import Opportunity, Company
    tally = {}
    for oc in OpportunityCompetitor.query.all():
        opp_id = getattr(oc, 'opportunity_id', None)
        cust_name = '(no opp)'
        if opp_id:
            opp = Opportunity.query.get(opp_id)
            if opp and opp.company_id:
                co = Company.query.get(opp.company_id)
                cust_name = co.name if co else cust_name
        cm = CompetitorMaster.query.get(oc.competitor_id) \
            if getattr(oc, 'competitor_id', None) else None
        comp = cm.name if cm else '(unknown)'
        tally[(cust_name, comp)] = tally.get((cust_name, comp), 0) + 1
    rows = [{'customer': k[0], 'competitor': k[1], 'encounters': v}
            for k, v in sorted(tally.items())]
    return _serve(rows, {
        'slug': 'competitor_by_customer',
        'title': 'Competitor by Customer',
        'category': 'Competitor',
        'columns': [
            {'key': 'customer', 'label': 'Customer'},
            {'key': 'competitor', 'label': 'Competitor'},
            {'key': 'encounters', 'label': 'Encounters'},
        ],
    }, dict(request.args))


@bp.route('/reports/competitor-by-industry')
@_require_auth
def rep_competitor_by_industry():
    return _competitor_grouping(
        'industry', 'Industry',
        lambda cm: getattr(cm, 'verticals', []) or [])


@bp.route('/reports/competitor-by-vertical')
@_require_auth
def rep_competitor_by_vertical():
    return _competitor_grouping(
        'vertical', 'Vertical',
        lambda cm: getattr(cm, 'verticals', []) or [])


@bp.route('/reports/competitor-by-service')
@_require_auth
def rep_competitor_by_service():
    return _competitor_grouping(
        'service', 'Service',
        lambda cm: getattr(cm, 'services', []) or [])


@bp.route('/reports/competitor-by-geography')
@_require_auth
def rep_competitor_by_geography():
    return _competitor_grouping(
        'geography', 'Geography',
        lambda cm: getattr(cm, 'geographic_reach', []) or
                   [getattr(cm, 'country', '')])


# =========================================================================
# Account Development (spec §61)
# =========================================================================
@bp.route('/reports/accounts-by-pic')
@_require_auth
def rep_accounts_by_pic():
    from app import Company
    tally = {}
    for c in Company.query.filter(Company.is_active.is_(True)).all():
        pic = c.pic_emp_code or '(unassigned)'
        tally[pic] = tally.get(pic, 0) + 1
    rows = [{'pic': k, 'accounts': v} for k, v in
            sorted(tally.items(), key=lambda kv: -kv[1])]
    return _serve(rows, {
        'slug': 'accounts_by_pic',
        'title': 'Accounts by PIC',
        'category': 'Account Development',
        'columns': [
            {'key': 'pic', 'label': 'PIC'},
            {'key': 'accounts', 'label': 'Accounts'},
        ],
    }, dict(request.args))


@bp.route('/reports/accounts-by-stage')
@_require_auth
def rep_accounts_by_stage():
    from app import Company
    tally = {}
    for c in Company.query.filter(Company.is_active.is_(True)).all():
        st = c.dev_stage or '(unset)'
        tally[st] = tally.get(st, 0) + 1
    rows = [{'stage': k, 'accounts': v} for k, v in
            sorted(tally.items(), key=lambda kv: -kv[1])]
    return _serve(rows, {
        'slug': 'accounts_by_stage',
        'title': 'Accounts by Development Stage',
        'category': 'Account Development',
        'columns': [
            {'key': 'stage', 'label': 'Stage'},
            {'key': 'accounts', 'label': 'Accounts'},
        ],
    }, dict(request.args))


@bp.route('/reports/activities-by-account')
@_require_auth
def rep_activities_by_account():
    # Lead.company is a plain account-name string, not a FK — unlike
    # Opportunity.company_id.  Group on the name directly.
    from app import LeadActivity, Lead
    q = (db.session.query(Lead.company, func.count(LeadActivity.id))
         .join(LeadActivity, LeadActivity.lead_id == Lead.id)
         .group_by(Lead.company))
    rows = [{'account': (name or '').strip() or '(no account)',
             'activities': int(n)}
            for name, n in q.all()]
    rows.sort(key=lambda r: -r['activities'])
    return _serve(rows, {
        'slug': 'activities_by_account',
        'title': 'Activities per Account',
        'category': 'Account Development',
        'columns': [
            {'key': 'account', 'label': 'Account'},
            {'key': 'activities', 'label': 'Activities'},
        ],
    }, dict(request.args))


@bp.route('/reports/contacts-developed')
@_require_auth
def rep_contacts_developed():
    from app import Contact
    tally = {}
    for c in Contact.query.filter(Contact.assigned_to.isnot(None)).all():
        who = c.assigned_to or '(unknown)'
        tally[who] = tally.get(who, 0) + 1
    rows = [{'user': k, 'contacts_developed': v}
            for k, v in sorted(tally.items(), key=lambda kv: -kv[1])]
    return _serve(rows, {
        'slug': 'contacts_developed',
        'title': 'Contacts Developed per Person',
        'category': 'Account Development',
        'columns': [
            {'key': 'user', 'label': 'User'},
            {'key': 'contacts_developed', 'label': 'Contacts'},
        ],
    }, dict(request.args))


@bp.route('/reports/rfqs-by-account')
@_require_auth
def rep_rfqs_by_account():
    try:
        from app.models.rfq import RFQ
    except Exception:
        return _serve([], {
            'slug': 'rfqs_by_account',
            'title': 'RFQs by Account',
            'category': 'Account Development',
            'columns': [{'key': 'account', 'label': 'Account'},
                        {'key': 'rfqs', 'label': 'RFQs'}],
        }, dict(request.args))
    from app import Company
    tally = {}
    for r in RFQ.query.all():
        cid = getattr(r, 'company_id', None) or \
              getattr(r, 'account_id', None)
        if not cid:
            continue
        tally[cid] = tally.get(cid, 0) + 1
    rows = []
    for cid, n in sorted(tally.items(), key=lambda kv: -kv[1]):
        c = Company.query.get(cid)
        rows.append({'account': c.name if c else '(unknown)', 'rfqs': n})
    return _serve(rows, {
        'slug': 'rfqs_by_account',
        'title': 'RFQs by Account',
        'category': 'Account Development',
        'columns': [
            {'key': 'account', 'label': 'Account'},
            {'key': 'rfqs', 'label': 'RFQs'},
        ],
    }, dict(request.args))


def _quote_agg(field):
    """Sum a numeric field on Quote grouped by account name.  Returns
    a list of ``{'account', field}`` dicts."""
    try:
        from app.models.quote import Quote
    except Exception:
        return []
    from app import Company
    tally = {}
    for q in Quote.query.all():
        cid = getattr(q, 'company_id', None) or \
              getattr(q, 'account_id', None)
        if not cid:
            continue
        val = getattr(q, field, None) or 0
        try:
            val = float(val)
        except Exception:
            val = 0.0
        tally[cid] = tally.get(cid, 0.0) + val
    rows = []
    for cid, n in sorted(tally.items(), key=lambda kv: -kv[1]):
        c = Company.query.get(cid)
        rows.append({'account': c.name if c else '(unknown)',
                     'total': round(n, 2)})
    return rows


@bp.route('/reports/quote-value-by-account')
@_require_auth
def rep_quote_value_by_account():
    rows = _quote_agg('total_value_inr')
    return _serve(rows, {
        'slug': 'quote_value_by_account',
        'title': 'Quoted Value by Account',
        'category': 'Account Development',
        'columns': [
            {'key': 'account', 'label': 'Account'},
            {'key': 'total', 'label': 'Total Quoted (INR)'},
        ],
    }, dict(request.args))


@bp.route('/reports/won-value-by-account')
@_require_auth
def rep_won_value_by_account():
    from app import Opportunity, Company
    tally = {}
    for o in Opportunity.query.filter(Opportunity.stage == 'Won').all():
        if not o.company_id:
            continue
        v = float(o.value_inr) if o.value_inr else 0.0
        tally[o.company_id] = tally.get(o.company_id, 0.0) + v
    rows = []
    for cid, v in sorted(tally.items(), key=lambda kv: -kv[1]):
        c = Company.query.get(cid)
        rows.append({'account': c.name if c else '(unknown)',
                     'won_inr': round(v, 2)})
    return _serve(rows, {
        'slug': 'won_value_by_account',
        'title': 'Won Value by Account',
        'category': 'Account Development',
        'columns': [
            {'key': 'account', 'label': 'Account'},
            {'key': 'won_inr', 'label': 'Won Value (INR)'},
        ],
    }, dict(request.args))


@bp.route('/reports/overseas-partner-contribution')
@_require_auth
def rep_overseas_partner_contribution():
    from app import OverseasAgent, Lead
    tally = {}
    for a in OverseasAgent.query.all():
        n = Lead.query.filter(Lead.source_details.ilike(f'%{a.name}%'))\
                      .count() if hasattr(Lead, 'source_details') else 0
        tally[a.name] = n
    rows = [{'partner': k, 'leads': v} for k, v in
            sorted(tally.items(), key=lambda kv: -kv[1])]
    return _serve(rows, {
        'slug': 'overseas_partner_contribution',
        'title': 'Overseas Partner Contribution',
        'category': 'Account Development',
        'columns': [
            {'key': 'partner', 'label': 'Overseas Partner'},
            {'key': 'leads', 'label': 'Leads'},
        ],
    }, dict(request.args))


@bp.route('/reports/network-contribution')
@_require_auth
def rep_network_contribution():
    from app import Lead
    tally = {}
    for l in Lead.query.all():
        src = getattr(l, 'source', None) or '(unset)'
        tally[src] = tally.get(src, 0) + 1
    rows = [{'source': k, 'leads': v} for k, v in
            sorted(tally.items(), key=lambda kv: -kv[1])]
    return _serve(rows, {
        'slug': 'network_contribution',
        'title': 'Lead Source Network Contribution',
        'category': 'Account Development',
        'columns': [
            {'key': 'source', 'label': 'Source'},
            {'key': 'leads', 'label': 'Leads'},
        ],
    }, dict(request.args))


@bp.route('/reports/dormant-accounts')
@_require_auth
def rep_dormant_accounts():
    from app import Company
    days = int(request.args.get('days') or 90)
    threshold = datetime.utcnow() - timedelta(days=days)
    q = Company.query.filter(
        Company.is_active.is_(True),
        or_(Company.last_activity_at.is_(None),
            Company.last_activity_at < threshold),
    ).order_by(Company.last_activity_at.asc().nullsfirst())
    rows = [{
        'id': c.id, 'name': c.name,
        'pic': c.pic_emp_code or '',
        'last_activity_at': str(c.last_activity_at)[:16]
            if c.last_activity_at else '(never)',
        'priority': c.priority or '',
    } for c in q.all()]
    return _serve(rows, {
        'slug': 'dormant_accounts',
        'title': f'Dormant Accounts (> {days} days)',
        'category': 'Account Development',
        'columns': [
            {'key': 'id', 'label': 'ID'},
            {'key': 'name', 'label': 'Account'},
            {'key': 'pic', 'label': 'PIC'},
            {'key': 'last_activity_at', 'label': 'Last Activity'},
            {'key': 'priority', 'label': 'Priority'},
        ],
    }, dict(request.args))


# =========================================================================
# Master index (used by the hub)
# =========================================================================
REPORT_INDEX = [
    ('Action Management', [
        ('rep_open_tasks', 'Open Tasks'),
        ('rep_overdue_tasks', 'Overdue Tasks'),
        ('rep_tasks_by_user', 'Tasks by User'),
        ('rep_tasks_by_role', 'Tasks by Role'),
        ('rep_tasks_by_vertical', 'Tasks by Vertical'),
        ('rep_task_completion', 'Task Completion Throughput'),
        ('rep_sla_performance', 'SLA Performance (all)'),
        ('rep_sla_rate_sourcing', 'SLA — Rate Sourcing'),
        ('rep_sla_quote_prep', 'SLA — Quote Prep'),
        ('rep_sla_quote_submit', 'SLA — Quote Submit'),
        ('rep_sla_negotiation_followup', 'SLA — Negotiation Follow-up'),
        ('rep_sla_won_handover', 'SLA — Won → Handover'),
        ('rep_account_followup_due', 'Accounts — Follow-up Due'),
        ('rep_project_review_due', 'Project Reviews Due'),
    ]),
    ('Competitor', [
        ('rep_competitor_register', 'Competitor Register'),
        ('rep_competitor_contacts', 'Competitor Contacts'),
        ('rep_competitor_intel_log', 'Competitor Intelligence Log'),
        ('rep_competitor_encounters', 'Competitor Encounters'),
        ('rep_win_loss_by_competitor', 'Win/Loss by Competitor'),
        ('rep_price_comparison', 'Price Comparison'),
        ('rep_competitor_by_customer', 'Competitor by Customer'),
        ('rep_competitor_by_industry', 'Competitor by Industry'),
        ('rep_competitor_by_vertical', 'Competitor by Vertical'),
        ('rep_competitor_by_service', 'Competitor by Service'),
        ('rep_competitor_by_geography', 'Competitor by Geography'),
    ]),
    ('Account Development', [
        ('rep_accounts_by_pic', 'Accounts by PIC'),
        ('rep_accounts_by_stage', 'Accounts by Stage'),
        ('rep_activities_by_account', 'Activities per Account'),
        ('rep_contacts_developed', 'Contacts Developed'),
        ('rep_rfqs_by_account', 'RFQs by Account'),
        ('rep_quote_value_by_account', 'Quoted Value by Account'),
        ('rep_won_value_by_account', 'Won Value by Account'),
        ('rep_overseas_partner_contribution',
         'Overseas Partner Contribution'),
        ('rep_network_contribution', 'Network Contribution'),
        ('rep_dormant_accounts', 'Dormant Accounts'),
    ]),
]
