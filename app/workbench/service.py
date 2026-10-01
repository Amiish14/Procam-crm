"""
The Daily Workbench: everything one person has to act on, in one list.

Built from `app.services.sales_rules` — the conditions, groups,
thresholds and ageing buckets all live there, so the Workbench, the
Team Workbench and (later) the daily email cannot drift apart.

Shape of the work:
  * one scoped, column-trimmed read of the open leads the viewer may see
  * two grouped reads for "when was this customer last contacted"
  * one read of the RFQs awaiting a quote, one of the quotes against them
  * one read per hygiene check that applies to leads, for the data issues
  * accounts needing development, one read

Then the conditions are evaluated in Python over those rows. The
alternative — a UNION of a dozen filtered queries — costs more reads and
cannot rank a lead that trips four conditions at once, which is exactly
the lead that should be at the top.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy.orm import load_only

from app.services import sales_rules as rules

#: Hygiene checks that describe a lead. Reused from Data Quality rather
#: than re-expressed here, so a threshold is changed in one place.
LEAD_HYGIENE_CHECKS = (
    'unowned_leads', 'leads_of_leavers', 'stale_leads', 'leads_no_followup',
    'unlinked_leads', 'lead_unknown_vertical', 'quoted_without_quote',
    'won_leads_no_value', 'quote_past_validity', 'lead_account_mismatch',
)

#: Columns the Workbench reads off a lead. Loading whole Lead objects
#: pulls every email body and extracted-JSON blob for thousands of rows.
_LEAD_COLUMNS = (
    'id', 'company', 'company_id', 'project', 'stage', 'procam_vertical',
    'assigned_to', 'assigned_name', 'secondary_owner', 'followup_date',
    'next_action', 'estimated_value_inr', 'quoted_amount_inr', 'cost_million',
    'quote_date', 'quote_validity_date', 'quote_no', 'rfq_date', 'opp_number',
    'created_at', 'updated_at', 'stage_entered_at', 'is_archived',
)


def _today():
    return rules.business_today()


def _lead_rows(sc):
    from app import Lead
    q = rules.open_leads(sc=sc).options(
        load_only(*[getattr(Lead, c) for c in _LEAD_COLUMNS]))
    return q.all()


def _hygiene_ids(sc):
    """{lead_id: [check title, ...]} for the lead-level checks."""
    from app.data_quality import service as dq
    out = {}
    for key in LEAD_HYGIENE_CHECKS:
        check = dq.find(key)
        if check is None:
            continue
        try:
            _count, detail = dq.run(check, sc)
        except Exception:
            from app import db
            db.session.rollback()
            continue
        ids = []
        if detail is None:
            continue
        if hasattr(detail, 'with_entities'):
            from app import Lead
            try:
                ids = [r[0] for r in detail.with_entities(Lead.id).limit(2000)]
            except Exception:
                from app import db
                db.session.rollback()
                ids = []
        elif isinstance(detail, (list, tuple)):
            for row in detail:
                row = row[0] if isinstance(row, tuple) else row
                if getattr(row, 'id', None) and type(row).__name__ == 'Lead':
                    ids.append(row.id)
        for lid in ids:
            out.setdefault(lid, []).append(check.label)
    return out


def _rfq_by_lead(sc, today):
    """{lead_id: the most pressing RFQ row} from the ageing service."""
    ageing = rules.rfq_ageing(sc=sc, today=today)
    by_lead = {}
    for row in ageing['buckets'].values():
        for r in row:
            if not r['lead_id']:
                continue
            kept = by_lead.get(r['lead_id'])
            if kept is None or (r['age_days'] or 0) > (kept['age_days'] or 0):
                by_lead[r['lead_id']] = r
    return by_lead, ageing


def _account_rows(sc, today):
    """Accounts the viewer owns that need development attention."""
    from app import Company
    from app.access import scope as sc_mod
    quiet_before = datetime.utcnow() - timedelta(days=rules.ACCOUNT_QUIET_DAYS)
    q = sc_mod.companies(Company.query.filter(Company.is_active.is_(True)),
                         sc=sc)
    rows = []
    for c in q.options(load_only(
            Company.id, Company.name, Company.vertical, Company.pic_emp_code,
            Company.secondary_pic_emp_code, Company.dev_stage,
            Company.last_activity_at, Company.next_action_at)).all():
        due = c.next_action_at is not None and c.next_action_at <= today
        quiet = (c.last_activity_at is None
                 or c.last_activity_at < quiet_before)
        if not (due or quiet):
            continue
        rows.append({'company': c, 'due': due, 'quiet': quiet})
    return rows


def _priority_and_due(conditions, lead, today, rfq):
    """Where this record sits in the list, and the date it is judged on."""
    due, priority = None, rules.P_ROUTINE
    if 'followup_overdue' in conditions:
        due, priority = lead.followup_date, rules.P_OVERDUE
    elif 'quote_overdue' in conditions and rfq:
        due, priority = rfq['quote_due'], rules.P_OVERDUE
    elif 'quote_lapsed' in conditions:
        due, priority = lead.quote_validity_date, rules.P_OVERDUE
    elif 'followup_today' in conditions:
        due, priority = lead.followup_date, rules.P_TODAY
    elif 'quote_due_soon' in conditions and rfq:
        due, priority = rfq['quote_due'], rules.P_TODAY
    elif 'stale' in conditions:
        priority = rules.P_SOON
    elif 'idle' in conditions or 'negotiation_idle' in conditions:
        priority = rules.P_SOON
    elif lead.followup_date:
        due = lead.followup_date
    if 'high_value' in conditions and priority > rules.P_SOON:
        priority = rules.P_SOON
    return (str(due) if due else ''), priority


def _primary_group(conditions):
    """The group a record is filed under when it trips several."""
    for key, _label, group, _why in rules.CONDITIONS:
        if key in conditions:
            return group
    return rules.G_DATA


def lead_items(sc, *, today=None):
    """Every lead that needs attention, as action items."""
    from app.services import lead_value
    today = today or _today()
    leads = _lead_rows(sc)
    idle = rules.idle_days_map([l.id for l in leads])
    hygiene = _hygiene_ids(sc)
    rfq_by_lead, ageing = _rfq_by_lead(sc, today)
    now = datetime.utcnow()
    items = []
    for l in leads:
        conds, rfq = [], rfq_by_lead.get(l.id)
        contacted_days = idle.get(l.id)
        idle_days = rules.idle_days(l, contacted_days)
        if l.followup_date and l.followup_date < today:
            conds.append('followup_overdue')
        if l.followup_date == today:
            conds.append('followup_today')
        if rfq:
            if rfq['days_late']:
                conds.append('quote_overdue')
            elif rfq['quote_due'] and rules.days_between(
                    today, date.fromisoformat(rfq['quote_due'])) <= 2:
                conds.append('quote_due_soon')
        if l.stage in lead_value.OPEN_QUOTE_STAGES:
            if l.quoted_amount_inr is None or l.quote_date is None:
                conds.append('quote_missing_detail')
            if (l.quote_validity_date is not None
                    and l.quote_validity_date < today):
                conds.append('quote_lapsed')
        if (l.stage == 'Under Negotiation'
                and idle_days is not None and idle_days >= rules.IDLE_DAYS):
            conds.append('negotiation_idle')
        if idle_days is not None and idle_days >= rules.STALE_DAYS:
            conds.append('stale')
        elif idle_days is not None and idle_days >= rules.IDLE_DAYS:
            conds.append('idle')
        if not l.followup_date:
            conds.append('no_next_action')
        if l.id in hygiene:
            conds.append('data_issue')
        if (l.created_at and
                (now - l.created_at) <= timedelta(hours=rules.NEW_ASSIGNMENT_HOURS)):
            conds.append('newly_assigned')
        if rules.is_high_value(l):
            conds.append('high_value')
        if not conds:
            continue
        due, priority = _priority_and_due(conds, l, today, rfq)
        value = lead_value.value_inr(l)
        items.append({
            'kind': 'lead',
            'id': l.id,
            'group': _primary_group(conds),
            'priority': priority,
            'due': due,
            'account': l.company or '',
            'account_id': l.company_id,
            'title': l.project or l.company or f'Lead #{l.id}',
            'vertical': l.procam_vertical or '',
            'stage': l.stage or '',
            'value_inr': float(value) if value is not None else None,
            'last_activity_days': contacted_days,
            'quiet_days': idle_days,
            'next_action': l.next_action or '',
            'next_action_date': str(l.followup_date) if l.followup_date else '',
            'quote_due': (rfq or {}).get('quote_due', ''),
            'quote_number': l.quote_no or (rfq or {}).get('rfq_number', ''),
            'ageing_days': rules.days_between(
                (l.stage_entered_at or l.updated_at or l.created_at), today),
            'assigned_to': l.assigned_to or '',
            'assigned_name': l.assigned_name or l.assigned_to or '',
            'data_issues': hygiene.get(l.id, []),
            'conditions': conds,
            'reasons': [rules.CONDITION_LABELS.get(c, c) for c in conds],
            'route': f'/app?lead={l.id}',
        })
    return items, ageing


def account_items(sc, *, today=None):
    today = today or _today()
    out = []
    for row in _account_rows(sc, today):
        c = row['company']
        conds = []
        if row['due']:
            conds.append('account_next_action')
        if row['quiet']:
            conds.append('account_quiet')
        out.append({
            'kind': 'account',
            'id': c.id,
            'group': rules.G_ACCOUNTS,
            'priority': rules.P_TODAY if row['due'] else rules.P_ROUTINE,
            'due': str(c.next_action_at) if c.next_action_at else '',
            'account': c.name,
            'account_id': c.id,
            'title': c.name,
            'vertical': c.vertical or '',
            'stage': c.dev_stage or '',
            'value_inr': None,
            'last_activity_days': rules.days_between(c.last_activity_at, today),
            'next_action': '',
            'next_action_date': str(c.next_action_at) if c.next_action_at else '',
            'quote_due': '', 'quote_number': '',
            'ageing_days': rules.days_between(c.last_activity_at, today),
            'assigned_to': c.pic_emp_code or '',
            'assigned_name': c.pic_emp_code or '',
            'data_issues': [],
            'conditions': conds,
            'reasons': [rules.CONDITION_LABELS.get(c_, c_) for c_ in conds],
            'route': f'/companies/{c.id}',
        })
    return out


def task_items(sc, *, today=None):
    """Open task-engine tasks, on the same board as everything else.

    The CRM had two "my work" screens — the task buckets and a separate
    lead-pendency API — and a person had to read both. One board, one
    answer.
    """
    from app.models.task_engine import TaskInstance, TaskInstanceStatus
    today = today or _today()
    now = datetime.utcnow()
    q = TaskInstance.query.filter(
        TaskInstance.status.in_(TaskInstanceStatus.OPEN))
    if not sc.unrestricted:
        codes = sc.codes or set()
        q = q.filter(TaskInstance.owner_user_id.in_(codes or {''}))
    out = []
    for t in q.order_by(TaskInstance.due_at.asc().nullslast()).limit(500):
        overdue = bool(t.due_at and t.due_at < now)
        due_today = bool(t.due_at and not overdue
                         and t.due_at.date() == today)
        key = (t.task_key or '')
        group = (rules.G_QUOTES if key.startswith(('quote.', 'rfq.'))
                 else rules.G_ACCOUNTS if key.startswith('account.')
                 else rules.G_TODAY)
        conds = (['followup_overdue'] if overdue
                 else ['followup_today'] if due_today else [])
        out.append({
            'kind': 'task',
            'id': t.id,
            'group': group,
            'priority': (rules.P_OVERDUE if overdue
                         else rules.P_TODAY if due_today
                         else min(rules.P_ROUTINE, max(1, t.priority or 3))),
            'due': str(t.due_at)[:10] if t.due_at else '',
            'account': t.entity_display or '',
            'account_id': None,
            'title': key.replace('.', ' ').replace('_', ' ').title(),
            'vertical': '', 'stage': t.status or '',
            'value_inr': None,
            'last_activity_days': None,
            'next_action': t.entity_display or '',
            'next_action_date': str(t.due_at)[:10] if t.due_at else '',
            'quote_due': '', 'quote_number': '',
            'ageing_days': int((now - t.created_at).days) if t.created_at else None,
            'assigned_to': t.owner_user_id or '',
            'assigned_name': t.owner_user_id or (t.owner_role or ''),
            'data_issues': [],
            'conditions': conds,
            'reasons': [rules.CONDITION_LABELS.get(c, c) for c in conds]
                       or [t.status or 'Open task'],
            'route': t.action_route or '/my-work',
        })
    return out


def _sort_key(item):
    return (item['priority'], item['due'] or '9999-12-31',
            -(item['value_inr'] or 0))


def board(sc, *, today=None, group=None, search='', vertical='', stage='',
          owner='', page=1, per_page=50, sort='priority'):
    """The whole Workbench: counters, the filtered page of items, and
    the quote-ageing buckets."""
    today = today or _today()
    items, ageing = lead_items(sc, today=today)
    items += account_items(sc, today=today)
    items += task_items(sc, today=today)

    counts = {key: 0 for key, _l in rules.GROUPS}
    summary = {
        'due_today': 0, 'overdue': 0, 'rfqs_awaiting_quote': ageing['total'],
        'quotes_due': 0, 'followups_due': 0, 'stale': 0, 'no_next_action': 0,
        'new_assignments': 0, 'data_updates': 0, 'high_value': 0,
        'quotes_overdue': ageing['overdue_count'],
    }
    for it in items:
        counts[it['group']] = counts.get(it['group'], 0) + 1
        c = it['conditions']
        if 'followup_today' in c or 'account_next_action' in c:
            summary['due_today'] += 1
        if 'followup_overdue' in c or 'quote_overdue' in c or 'quote_lapsed' in c:
            summary['overdue'] += 1
        if 'quote_due_soon' in c or 'quote_overdue' in c:
            summary['quotes_due'] += 1
        if 'followup_today' in c or 'followup_overdue' in c:
            summary['followups_due'] += 1
        if 'stale' in c:
            summary['stale'] += 1
        if 'no_next_action' in c:
            summary['no_next_action'] += 1
        if 'newly_assigned' in c:
            summary['new_assignments'] += 1
        if 'data_issue' in c or 'quote_missing_detail' in c:
            summary['data_updates'] += 1
        if 'high_value' in c:
            summary['high_value'] += 1

    rows = items
    if group:
        rows = [r for r in rows if r['group'] == group]
    if vertical:
        rows = [r for r in rows if r['vertical'] == vertical]
    if stage:
        rows = [r for r in rows if r['stage'] == stage]
    if owner:
        rows = [r for r in rows if r['assigned_to'] == owner]
    if search:
        s = search.lower()
        rows = [r for r in rows
                if s in (r['account'] or '').lower()
                or s in (r['title'] or '').lower()]
    if sort == 'value':
        rows.sort(key=lambda r: -(r['value_inr'] or 0))
    elif sort == 'ageing':
        rows.sort(key=lambda r: -(r['ageing_days'] or 0))
    elif sort == 'due':
        rows.sort(key=lambda r: r['due'] or '9999-12-31')
    else:
        rows.sort(key=_sort_key)

    total = len(rows)
    per_page = max(1, min(int(per_page or 50), 200))
    page = max(1, int(page or 1))
    start = (page - 1) * per_page
    return {
        'summary': summary,
        'group_counts': counts,
        'groups': [{'key': k, 'label': lbl, 'count': counts.get(k, 0)}
                   for k, lbl in rules.GROUPS],
        'items': rows[start:start + per_page],
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': max(1, (total + per_page - 1) // per_page),
        'quote_ageing': {
            'counts': ageing['counts'],
            'overdue_count': ageing['overdue_count'],
            'buckets': [{'key': k, 'label': lbl,
                         'count': ageing['counts'].get(k, 0)}
                        for k, lbl, _lo, _hi in rules.AGEING_BUCKETS],
        },
    }
