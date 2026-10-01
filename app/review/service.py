"""
The sales review, measured.

Every threshold, stage list and ageing bucket in this module is read
from `app.services.sales_rules`; every record set is read through
`app.access.scope`. The module adds no KPI of its own — it arranges
existing ones into the three shapes a review needs:

    individual()   one person over a period
    vertical()     one desk over a period, with the drill-through
    meeting()      the agenda, in the order the meeting runs

plus the actions a meeting agrees (`app/models/review.py`), which the
*next* meeting opens with until they are resolved.

Why the figures are computed in Python over a scoped, column-trimmed
read rather than in SQL: the same row has to be counted several ways
(open, idle, high value, missing a next action) and then listed with the
reason it was counted. A review whose number cannot be opened is a
number nobody believes.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.orm import load_only

from app.access import scope as sc_mod
from app.models.access import DataScope
from app.models.review import ReviewAction, ReviewActionStatus
from app.services import lead_value, sales_rules as rules
from app.workbench import service as wb

#: How far back the trend lines look. A week against the week before and
#: a month against the month before is what a review asks for; anything
#: longer is a report, not a review.
WOW_DAYS = 7
MOM_DAYS = 30

#: How many rows a list section carries. A meeting cannot read more.
LIST_LIMIT = 25

#: Lead columns the review reads. Whole Lead objects drag every email
#: body and extracted-JSON blob behind them.
_LEAD_COLUMNS = (
    'id', 'company', 'company_id', 'project', 'stage', 'procam_vertical',
    'assigned_to', 'assigned_name', 'secondary_owner', 'followup_date',
    'next_action', 'estimated_value_inr', 'quoted_amount_inr',
    'quote_date', 'quote_validity_date', 'quote_no', 'rfq_date',
    'lost_reason', 'opp_close_date', 'created_at', 'updated_at',
    'stage_entered_at', 'is_archived',
)


class ReviewRefused(Exception):
    """The viewer may not review this subject. Carries the HTTP status so
    a route can answer without re-deciding."""

    def __init__(self, message, status=403):
        super().__init__(message)
        self.message = message
        self.status = status


# ── periods ──────────────────────────────────────────────────────────
#: The named periods a meeting runs on.
PERIODS = ('week', 'fortnight', 'month', 'quarter')
_PERIOD_DAYS = {'week': 7, 'fortnight': 14, 'month': 30, 'quarter': 90}


def _as_date(value, fallback=None):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = (str(value or '')).strip()[:10]
    if not text:
        return fallback
    try:
        return datetime.strptime(text, '%Y-%m-%d').date()
    except ValueError:
        return fallback


def period_dates(start=None, end=None, period=None):
    """(start, end) inclusive. Explicit dates win; otherwise the named
    period counted back from today where the business is."""
    today = rules.business_today()
    end_d = _as_date(end, today)
    start_d = _as_date(start)
    if start_d is None:
        days = _PERIOD_DAYS.get((period or 'month').lower(), 30)
        start_d = end_d - timedelta(days=days - 1)
    if start_d > end_d:
        start_d, end_d = end_d, start_d
    return start_d, end_d


def _within(day, start, end):
    day = _as_date(day)
    return day is not None and start <= day <= end


# ── scope ────────────────────────────────────────────────────────────
def _viewer(sc=None):
    return sc if sc is not None else sc_mod.current()


def narrowed(sc, codes):
    """The viewer's boundary, confined to some of the people inside it.

    Narrowing only: `codes` is intersected with what the viewer already
    reaches, so no caller can widen a scope by passing a longer list.
    """
    codes = {c for c in codes if c}
    if not sc.unrestricted:
        codes &= set(sc.codes or ())
    return sc_mod.Scope(sc.emp_code, sc.vertical, codes, sc.perms,
                        sc.data_scope)


def may_review_person(emp_code, sc=None):
    sc = _viewer(sc)
    emp_code = (emp_code or '').strip()
    if not emp_code:
        return False
    if emp_code == (sc.emp_code or ''):
        return True
    return sc.unrestricted or sc.reaches(emp_code)


def require_person(emp_code, sc=None):
    """The scope to measure one person in, or a refusal.

    Refusing is deliberate: a manager asking about somebody outside their
    boundary must be told so, never handed an empty review that reads as
    "this person did nothing".
    """
    sc = _viewer(sc)
    emp_code = (emp_code or '').strip()
    if not emp_code:
        raise ReviewRefused('Say whose review this is', status=400)
    if not may_review_person(emp_code, sc):
        raise ReviewRefused('That person is outside your access')
    return narrowed(sc, {emp_code}) if not sc.unrestricted else \
        sc_mod.Scope(sc.emp_code, sc.vertical, {emp_code}, sc.perms,
                     sc.data_scope)


def may_review_vertical(vertical, sc=None):
    sc = _viewer(sc)
    vertical = (vertical or '').strip()
    if not vertical:
        return False
    if sc.unrestricted:
        return True
    # A desk review is about other people's work, so owning your own
    # records is not enough to open one — matching the Access Matrix,
    # which is what every other manager screen reads.
    if (sc.data_scope or DataScope.OWN) == DataScope.OWN:
        return False
    return vertical.lower() == (sc.vertical or '').lower()


def require_vertical(vertical, sc=None):
    sc = _viewer(sc)
    vertical = (vertical or '').strip()
    if not vertical:
        raise ReviewRefused('Say which vertical this review is for',
                            status=400)
    if not may_review_vertical(vertical, sc):
        raise ReviewRefused('That vertical is outside your access')
    return sc


def parse_scope_key(scope_key):
    """'emp:ABC' / 'vertical:Project Freight' → ('emp', 'ABC')."""
    text = (scope_key or '').strip()
    kind, _, value = text.partition(':')
    kind = kind.strip().lower()
    value = value.strip()
    if kind == 'emp':
        return 'emp', value.upper()
    if kind == 'vertical':
        return 'vertical', value
    raise ReviewRefused('A review is of a person (emp:CODE) or a vertical '
                        '(vertical:Name)', status=400)


def require_scope_key(scope_key, sc=None):
    """(kind, value, scope to measure in) for a review scope key."""
    kind, value = parse_scope_key(scope_key)
    if kind == 'emp':
        return kind, value, require_person(value, sc)
    require_vertical(value, sc)
    return kind, value, _vertical_scope(value, _viewer(sc))


def _vertical_scope(vertical, sc):
    """The people of one vertical, inside the viewer's boundary.

    A vertical is a property of the employee master, so the boundary is
    their codes — not `Lead.procam_vertical`, which is what the lead was
    tagged with and drifts.
    """
    from app import Employee
    q = Employee.query.filter(Employee.vertical.isnot(None))
    codes = {e.emp_code for e in q.all()
             if (e.vertical or '').strip().lower() == vertical.strip().lower()
             and e.emp_code}
    if sc.unrestricted:
        return sc_mod.Scope(sc.emp_code, vertical, codes, sc.perms,
                            sc.data_scope)
    return narrowed(sc, codes)


# ── shared reads ─────────────────────────────────────────────────────
def _lead_query(sc):
    from app import Lead
    return sc_mod.leads(Lead.query.filter(Lead.is_archived.isnot(True)),
                        sc=sc)


def _lead_rows(sc, vertical=None):
    """Every unarchived lead in the boundary, trimmed to the columns the
    review reads."""
    from app import Lead
    q = _lead_query(sc)
    if vertical:
        q = q.filter(Lead.procam_vertical == vertical)
    return q.options(load_only(*[getattr(Lead, c)
                                 for c in _LEAD_COLUMNS])).all()


def _value(lead):
    v = lead_value.value_inr(lead)
    return float(v) if v is not None else 0.0


def _closed_on(lead):
    """The day a lead reached its present stage.

    `stage_entered_at` is written when the stage changes; `updated_at` is
    the fallback for rows that pre-date it. Neither is a contact date, so
    neither is used for anything but "when did this close".
    """
    return _as_date(lead.stage_entered_at or lead.updated_at)


def _lead_row(lead, *, quiet_days=None):
    """One lead as a review list shows it, with the way through to it."""
    return {
        'id': lead.id,
        'account': lead.company or '',
        'account_id': lead.company_id,
        'title': lead.project or lead.company or f'Lead #{lead.id}',
        'stage': lead.stage or '',
        'vertical': lead.procam_vertical or '',
        'owner': lead.assigned_to or '',
        'owner_name': lead.assigned_name or lead.assigned_to or '',
        'value_inr': _value(lead),
        'value_display': lead_value.format_inr(_value(lead) or None),
        'quote_date': str(lead.quote_date) if lead.quote_date else '',
        'next_action': lead.next_action or '',
        'next_action_date': (str(lead.followup_date)
                             if lead.followup_date else ''),
        'expected_close': (str(lead.opp_close_date)
                           if lead.opp_close_date else ''),
        'lost_reason': lead.lost_reason or '',
        'quiet_days': quiet_days,
        'age_days': rules.days_between(lead.created_at),
        'route': f'/app?lead={lead.id}',
    }


def _sum(rows):
    return round(sum(r['value_inr'] for r in rows), 2)


def _section(key, title, rows, *, note=''):
    """An agenda section. Always present, even when empty: a review that
    silently drops "Lost" because there were none reads as an oversight."""
    return {'key': key, 'title': title, 'count': len(rows),
            'value_inr': _sum(rows) if rows and 'value_inr' in rows[0] else 0,
            'rows': rows[:LIST_LIMIT], 'truncated': len(rows) > LIST_LIMIT,
            'note': note}


# ── activity ─────────────────────────────────────────────────────────
#: Activity kinds that count as having been in front of the customer.
VISIT_KINDS = ('visit', 'meeting')


def _activity(codes, lead_ids, start, end):
    """What was recorded in the period: logged activities by these
    people, and emails on their leads.

    Contact is an activity OR an email — `app/services/contact.py`. The
    same two sources are counted here, so "12 touches" on the review and
    "last contacted" on the Workbench cannot disagree about what a touch
    is.
    """
    from app import LeadActivity, LeadEmail
    lo = datetime.combine(start, datetime.min.time())
    hi = datetime.combine(end, datetime.max.time())
    by_kind, leads_touched, visits = {}, set(), 0
    if codes:
        rows = (LeadActivity.query
                .with_entities(LeadActivity.kind, LeadActivity.lead_id)
                .filter(LeadActivity.performed_by.in_(codes or {''}),
                        LeadActivity.occurred_at >= lo,
                        LeadActivity.occurred_at <= hi).all())
        for kind, lead_id in rows:
            key = (kind or 'note').lower()
            by_kind[key] = by_kind.get(key, 0) + 1
            if key in VISIT_KINDS:
                visits += 1
            if lead_id:
                leads_touched.add(lead_id)
    emails = 0
    ids = [i for i in lead_ids if i]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        rows = (LeadEmail.query.with_entities(LeadEmail.lead_id)
                .filter(LeadEmail.lead_id.in_(chunk),
                        LeadEmail.sent_or_received_at >= lo,
                        LeadEmail.sent_or_received_at <= hi).all())
        emails += len(rows)
        leads_touched.update(r[0] for r in rows if r[0])
    total = sum(by_kind.values()) + emails
    return {'by_kind': by_kind, 'activities': sum(by_kind.values()),
            'emails': emails, 'visits': visits, 'total': total,
            'leads_touched': len(leads_touched)}


# ── funnel, commercial, discipline ───────────────────────────────────
def _funnel(leads, start, end):
    """The pipeline as it stands, and what moved through it."""
    open_stages = rules.open_stages()
    by_stage, open_rows = {}, []
    added, won, lost = [], [], []
    for l in leads:
        row = _lead_row(l)
        if (l.stage or '') in open_stages:
            open_rows.append(row)
            cell = by_stage.setdefault(l.stage or '—',
                                       {'stage': l.stage or '—', 'count': 0,
                                        'value_inr': 0.0})
            cell['count'] += 1
            cell['value_inr'] = round(cell['value_inr'] + row['value_inr'], 2)
        if _within(l.created_at, start, end):
            added.append(row)
        if l.stage == 'Won' and _within(_closed_on(l), start, end):
            won.append(row)
        if l.stage == 'Lost' and _within(_closed_on(l), start, end):
            lost.append(row)
    stages = [by_stage.get(s) or {'stage': s, 'count': 0, 'value_inr': 0.0}
              for s in open_stages]
    decided = len(won) + len(lost)
    return {
        'stages': stages,
        'open_count': len(open_rows),
        'open_value_inr': _sum(open_rows),
        'open_value_display': lead_value.format_inr(_sum(open_rows) or None),
        'added': added, 'added_count': len(added),
        'won': won, 'won_count': len(won), 'won_value_inr': _sum(won),
        'lost': lost, 'lost_count': len(lost), 'lost_value_inr': _sum(lost),
        'decided': decided,
        'conversion_pct': (round(len(won) / decided * 100, 1)
                           if decided else None),
        'open_rows': open_rows,
    }


def _commercial(sc, leads, start, end):
    """RFQs in, quotes out, and how long the customer waited."""
    from app.models.rfq import RFQ
    rfq_rows = (sc_mod.rfqs(RFQ.query.filter(RFQ.received_date >= start,
                                             RFQ.received_date <= end),
                            sc=sc).all())
    quoted = [l for l in leads if _within(l.quote_date, start, end)]
    # What was quoted, read through lead_value rather than off the
    # column: that module decides which figure is the quote.
    quote_value = round(sum(_value(l) for l in quoted
                            if lead_value.value_source(l) == 'quote'), 2)
    turnarounds = [rules.days_between(l.rfq_date, l.quote_date)
                   for l in quoted
                   if l.rfq_date and l.quote_date
                   and rules.days_between(l.rfq_date, l.quote_date) is not None
                   and rules.days_between(l.rfq_date, l.quote_date) >= 0]
    ageing = rules.rfq_ageing(sc=sc)
    return {
        'rfqs_received': len(rfq_rows),
        'rfqs_on_leads': len([l for l in leads
                              if _within(l.rfq_date, start, end)]),
        'rfqs_awaiting_quote': ageing['total'],
        'rfqs_overdue': ageing['overdue_count'],
        'quote_ageing': [{'key': k, 'label': lbl,
                          'count': ageing['counts'].get(k, 0)}
                         for k, lbl, _lo, _hi in rules.AGEING_BUCKETS],
        'quotes_sent': len(quoted),
        'quote_value_inr': quote_value,
        'quote_value_display': lead_value.format_inr(quote_value or None),
        'quote_turnaround_days': (round(sum(turnarounds) / len(turnarounds), 1)
                                  if turnarounds else None),
        'quote_turnaround_sample': len(turnarounds),
        'quote_sla_days': rules.QUOTE_SLA_DAYS,
        'quoted_rows': [_lead_row(l) for l in quoted],
        'rfq_rows': [{'id': r.id, 'rfq_number': r.rfq_number,
                      'subject': r.subject or '',
                      'received_date': (str(r.received_date)
                                        if r.received_date else ''),
                      'status': r.status or '',
                      'owner': r.lead_driver or '',
                      'route': f'/rfqs/{r.id}'} for r in rfq_rows],
    }


def _discipline(sc, leads, today):
    """Whether the CRM is being kept, measured with the Workbench's own
    rules so the review and the daily screen cannot disagree."""
    open_stages = rules.open_stages()
    open_leads = [l for l in leads if (l.stage or '') in open_stages]
    idle_map = rules.idle_days_map([l.id for l in open_leads])
    with_followup, overdue, no_next, stale, idle, never = 0, 0, 0, 0, 0, 0
    stale_rows, overdue_rows, no_next_rows = [], [], []
    for l in open_leads:
        quiet = rules.idle_days(l, idle_map.get(l.id))
        if idle_map.get(l.id) is None:
            never += 1
        if l.followup_date:
            with_followup += 1
            if l.followup_date < today:
                overdue += 1
                overdue_rows.append(_lead_row(l, quiet_days=quiet))
        else:
            no_next += 1
            no_next_rows.append(_lead_row(l, quiet_days=quiet))
        if quiet is not None and quiet >= rules.STALE_DAYS:
            stale += 1
            stale_rows.append(_lead_row(l, quiet_days=quiet))
        elif quiet is not None and quiet >= rules.IDLE_DAYS:
            idle += 1
    total = len(open_leads)
    return {
        'open_leads': total,
        'with_next_action': with_followup,
        'no_next_action': no_next,
        'followup_compliance_pct': (round(with_followup / total * 100, 1)
                                    if total else None),
        'overdue_followups': overdue,
        'idle_leads': idle,
        'stale_leads': stale,
        'never_contacted': never,
        'idle_days': rules.IDLE_DAYS,
        'stale_days': rules.STALE_DAYS,
        'stale_rows': stale_rows,
        'overdue_rows': overdue_rows,
        'no_next_action_rows': no_next_rows,
    }


def _data_quality(sc):
    """The hygiene checks that are failing inside this boundary.

    Counted by `app/data_quality/service.py`, never re-measured here —
    the thresholds are its to change.
    """
    from app.data_quality import service as dq
    rows = []
    try:
        summary = dq.summary(sc)
    except Exception:                                   # pragma: no cover
        from app import db
        db.session.rollback()
        return {'total': 0, 'checks': [], 'available': False}
    for row in summary:
        if row.get('count'):
            rows.append({'key': row.get('key'), 'title': row.get('title'),
                         'count': row.get('count'),
                         'severity': row.get('severity', ''),
                         'route': f"/data-quality/{row.get('key')}"})
    rows.sort(key=lambda r: -(r['count'] or 0))
    return {'total': sum(r['count'] for r in rows), 'checks': rows,
            'available': True}


def _accounts(sc, start, end, today):
    """The account book: who is being developed and who has gone quiet."""
    from app import Company
    quiet_before = datetime.utcnow() - timedelta(days=rules.ACCOUNT_QUIET_DAYS)
    rows = sc_mod.companies(Company.query.filter(
        Company.is_active.isnot(False)), sc=sc).options(
        load_only(Company.id, Company.name, Company.vertical,
                  Company.pic_emp_code, Company.secondary_pic_emp_code,
                  Company.dev_stage, Company.tier, Company.last_activity_at,
                  Company.next_action_at, Company.created_at)).all()
    def _row(c):
        return {'id': c.id, 'name': c.name,
                'vertical': c.vertical or '', 'tier': c.tier or '',
                'stage': c.dev_stage or '',
                'pic': c.pic_emp_code or '',
                'last_activity': (str(c.last_activity_at)[:10]
                                  if c.last_activity_at else ''),
                'quiet_days': rules.days_between(c.last_activity_at, today),
                'next_action_at': (str(c.next_action_at)
                                   if c.next_action_at else ''),
                'route': f'/companies/{c.id}'}
    quiet = [_row(c) for c in rows
             if c.last_activity_at is None or c.last_activity_at < quiet_before]
    new = [_row(c) for c in rows if _within(c.created_at, start, end)]
    active = [_row(c) for c in rows
              if c.last_activity_at and _within(c.last_activity_at, start, end)]
    due = [_row(c) for c in rows
           if c.next_action_at and c.next_action_at <= today]
    return {'total': len(rows), 'quiet': quiet, 'quiet_count': len(quiet),
            'new': new, 'new_count': len(new),
            'active': active, 'active_count': len(active),
            'action_due': due, 'action_due_count': len(due),
            'quiet_days_threshold': rules.ACCOUNT_QUIET_DAYS}


def _attention(sc):
    """What the Workbench says needs doing, as a review reads it.

    The board is the action matrix; re-deriving "overdue" here is exactly
    the drift this module exists to avoid.
    """
    try:
        board = wb.board(sc, per_page=1)
    except Exception:                                   # pragma: no cover
        from app import db
        db.session.rollback()
        return {'available': False, 'summary': {}, 'groups': []}
    return {'available': True, 'summary': board['summary'],
            'groups': board['groups'], 'total': board['total'],
            'route': '/my-work'}


# ── trends ───────────────────────────────────────────────────────────
def _measures(leads, codes, start, end):
    """The handful of figures a trend line is drawn through.

    Takes the leads already read rather than reading them again: four
    windows times one full scoped read was the whole cost of the page.
    """
    act = _activity(codes, [l.id for l in leads], start, end)
    funnel = _funnel(leads, start, end)
    quoted = [l for l in leads if _within(l.quote_date, start, end)]
    return {
        'touches': act['total'],
        'visits': act['visits'],
        'new_leads': funnel['added_count'],
        'quotes_sent': len(quoted),
        'won': funnel['won_count'],
        'won_value_inr': funnel['won_value_inr'],
    }


def _delta(current, previous):
    out = {}
    for key, now in current.items():
        was = previous.get(key) or 0
        change = round(now - was, 2)
        out[key] = {
            'current': now, 'previous': was, 'change': change,
            'change_pct': (round(change / was * 100, 1) if was else None),
            'direction': 'up' if change > 0 else 'down' if change < 0 else 'flat',
        }
    return out


def _trends(leads, codes, today):
    """This week against last, and this month against the one before."""
    out = {}
    for name, days in (('wow', WOW_DAYS), ('mom', MOM_DAYS)):
        cur_start = today - timedelta(days=days - 1)
        prev_end = cur_start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=days - 1)
        out[name] = {
            'days': days,
            'current_period': [str(cur_start), str(today)],
            'previous_period': [str(prev_start), str(prev_end)],
            'measures': _delta(_measures(leads, codes, cur_start, today),
                               _measures(leads, codes, prev_start, prev_end)),
        }
    return out


# ── individual review ────────────────────────────────────────────────
def individual(emp_code, start=None, end=None, sc=None, with_trends=True):
    """One person's review for a period.

    `sc` is the *viewer's* boundary. The subject's figures are measured
    inside a copy of it narrowed to that one person, so a manager can
    never see more of a colleague here than anywhere else.
    """
    from app import Employee
    sc = _viewer(sc)
    emp_code = (emp_code or '').strip().upper()
    subject_sc = require_person(emp_code, sc)
    start, end = period_dates(start, end)
    today = rules.business_today()

    emp = Employee.query.filter_by(emp_code=emp_code).first()
    leads = _lead_rows(subject_sc)
    codes = {emp_code}
    funnel = _funnel(leads, start, end)
    out = {
        'subject': {
            'emp_code': emp_code,
            'name': (emp.name if emp else '') or emp_code,
            'designation': (emp.designation or '') if emp else '',
            'vertical': (emp.vertical or '') if emp else '',
            'is_active': bool(emp.is_active) if emp else False,
            'route': f'/people/{emp_code}',
        },
        'period': {'start': str(start), 'end': str(end),
                   'days': (end - start).days + 1},
        'review_scope': f'emp:{emp_code}',
        'activity': _activity(codes, [l.id for l in leads], start, end),
        'funnel': funnel,
        'commercial': _commercial(subject_sc, leads, start, end),
        'discipline': _discipline(subject_sc, leads, today),
        'accounts': _accounts(subject_sc, start, end, today),
        'data_quality': _data_quality(subject_sc),
        'attention': _attention(subject_sc),
        'workload': _workload(emp_code),
        'actions': actions(f'emp:{emp_code}', sc=sc, status='unresolved'),
        'top_opportunities': sorted(funnel['open_rows'],
                                    key=lambda r: -r['value_inr'])[:LIST_LIMIT],
    }
    out['trends'] = _trends(leads, codes, today) if with_trends else {}
    return out


def _workload(emp_code):
    """What PIC 360 already says this person is carrying.

    Read rather than recomputed: the person's own page and their review
    must agree, and PIC 360 is where that total is defined.
    """
    from app.pic360 import service as p360
    try:
        return p360.workload(emp_code)
    except Exception:                                   # pragma: no cover
        from app import db
        db.session.rollback()
        return {}


# ── vertical review ──────────────────────────────────────────────────
def vertical(vertical_name, start=None, end=None, sc=None):
    """One desk's review, with the drill-through a review follows:
    vertical → person → account → opportunity."""
    sc = _viewer(sc)
    vertical_name = (vertical_name or '').strip()
    require_vertical(vertical_name, sc)
    vsc = _vertical_scope(vertical_name, sc)
    start, end = period_dates(start, end)
    today = rules.business_today()

    leads = _lead_rows(vsc)
    codes = set(vsc.codes or ())
    funnel = _funnel(leads, start, end)
    commercial = _commercial(vsc, leads, start, end)
    discipline = _discipline(vsc, leads, today)
    accounts = _accounts(vsc, start, end, today)

    return {
        'vertical': vertical_name,
        'review_scope': f'vertical:{vertical_name}',
        'period': {'start': str(start), 'end': str(end),
                   'days': (end - start).days + 1},
        'people': _people(vsc, leads, start, end),
        'pipeline': {'stages': funnel['stages'],
                     'open_count': funnel['open_count'],
                     'open_value_inr': funnel['open_value_inr'],
                     'open_value_display': funnel['open_value_display'],
                     'ageing': _opportunity_age(funnel['open_rows'], leads,
                                                today)},
        'rfqs': {'received': commercial['rfqs_received'],
                 'on_leads': commercial['rfqs_on_leads'],
                 'awaiting_quote': commercial['rfqs_awaiting_quote'],
                 'overdue': commercial['rfqs_overdue'],
                 'ageing': commercial['quote_ageing'],
                 'rows': commercial['rfq_rows'][:LIST_LIMIT]},
        'quotes': {'sent': commercial['quotes_sent'],
                   'value_inr': commercial['quote_value_inr'],
                   'value_display': commercial['quote_value_display'],
                   'turnaround_days': commercial['quote_turnaround_days'],
                   'turnaround_sample': commercial['quote_turnaround_sample'],
                   'sla_days': commercial['quote_sla_days'],
                   'rows': commercial['quoted_rows'][:LIST_LIMIT]},
        'results': {'won': funnel['won_count'],
                    'won_value_inr': funnel['won_value_inr'],
                    'lost': funnel['lost_count'],
                    'lost_value_inr': funnel['lost_value_inr'],
                    'conversion_pct': funnel['conversion_pct'],
                    'won_rows': funnel['won'][:LIST_LIMIT],
                    'lost_rows': funnel['lost'][:LIST_LIMIT]},
        'top_opportunities': sorted(funnel['open_rows'],
                                    key=lambda r: -r['value_inr'])[:LIST_LIMIT],
        'losses': _loss_reasons(funnel['lost']),
        'competitors': _competitors(leads, start, end),
        'accounts': {
            'total': accounts['total'],
            'top': _top_accounts(leads),
            'inactive': accounts['quiet'][:LIST_LIMIT],
            'inactive_count': accounts['quiet_count'],
            'new': accounts['new'][:LIST_LIMIT],
            'new_count': accounts['new_count'],
            'quiet_days_threshold': accounts['quiet_days_threshold'],
        },
        'visits': _visits(codes, start, end),
        'followup_compliance': {
            'open_leads': discipline['open_leads'],
            'with_next_action': discipline['with_next_action'],
            'no_next_action': discipline['no_next_action'],
            'compliance_pct': discipline['followup_compliance_pct'],
            'overdue': discipline['overdue_followups'],
            'stale': discipline['stale_leads'],
            'idle': discipline['idle_leads'],
            'rows': discipline['overdue_rows'][:LIST_LIMIT],
        },
        # The viewer's own boundary, not the desk's: a cross-sell signal
        # is by definition about another desk's work on the same account,
        # and narrowing to this desk's people would hide every case there
        # is. It still shows only what the viewer may already see.
        'cross_sell': _cross_sell(sc, vertical_name),
        'data_quality': _data_quality(vsc),
        'attention': _attention(vsc),
        'actions': actions(f'vertical:{vertical_name}', sc=sc,
                           status='unresolved'),
    }


def _people(sc, leads, start, end):
    """The desk, person by person — the first step of the drill-through."""
    from app import Employee
    by_code = {}
    for l in leads:
        code = l.assigned_to or ''
        cell = by_code.setdefault(code, {'emp_code': code, 'name': '',
                                         'open': 0, 'open_value_inr': 0.0,
                                         'won': 0, 'won_value_inr': 0.0,
                                         'lost': 0, 'new': 0})
        value = _value(l)
        if (l.stage or '') in rules.open_stages():
            cell['open'] += 1
            cell['open_value_inr'] = round(cell['open_value_inr'] + value, 2)
        closed = _closed_on(l)
        if l.stage == 'Won' and _within(closed, start, end):
            cell['won'] += 1
            cell['won_value_inr'] = round(cell['won_value_inr'] + value, 2)
        if l.stage == 'Lost' and _within(closed, start, end):
            cell['lost'] += 1
        if _within(l.created_at, start, end):
            cell['new'] += 1
    names = {e.emp_code: e.name for e in Employee.query.with_entities(
        Employee.emp_code, Employee.name).all()}
    rows = []
    for code, cell in by_code.items():
        cell['name'] = names.get(code) or code or 'Unassigned'
        cell['route'] = (f'/review/individual?emp={code}' if code else '')
        decided = cell['won'] + cell['lost']
        cell['conversion_pct'] = (round(cell['won'] / decided * 100, 1)
                                  if decided else None)
        rows.append(cell)
    rows.sort(key=lambda r: -r['open_value_inr'])
    return rows


def _opportunity_age(open_rows, leads, today):
    """How long the open pipeline has been open.

    Reported against the thresholds `sales_rules` already sets rather
    than a new set of buckets: anything past the stale mark is what a
    review argues about.
    """
    ages = [r['age_days'] for r in open_rows if r['age_days'] is not None]
    over_stale = [r for r in open_rows
                  if (r['age_days'] or 0) >= rules.STALE_DAYS]
    over_quiet = [r for r in open_rows
                  if (r['age_days'] or 0) >= rules.ACCOUNT_QUIET_DAYS]
    return {
        'average_days': round(sum(ages) / len(ages), 1) if ages else None,
        'oldest_days': max(ages) if ages else None,
        'over_stale_days': len(over_stale),
        'over_quiet_days': len(over_quiet),
        'stale_days': rules.STALE_DAYS,
        'quiet_days': rules.ACCOUNT_QUIET_DAYS,
        'oldest': sorted(open_rows, key=lambda r: -(r['age_days'] or 0)
                         )[:LIST_LIMIT],
    }


def _loss_reasons(lost_rows):
    """Why the desk lost, largest reason first."""
    by_reason = {}
    for row in lost_rows:
        key = row['lost_reason'] or 'Not recorded'
        cell = by_reason.setdefault(key, {'reason': key, 'count': 0,
                                          'value_inr': 0.0, 'rows': []})
        cell['count'] += 1
        cell['value_inr'] = round(cell['value_inr'] + row['value_inr'], 2)
        cell['rows'].append(row)
    out = sorted(by_reason.values(), key=lambda r: -r['count'])
    for cell in out:
        cell['rows'] = cell['rows'][:LIST_LIMIT]
    return out


def _competitors(leads, start, end):
    """Who we met, and how it went. Empty where nobody records it."""
    try:
        from app.models.competitor import (CompetitorMaster,
                                           OpportunityCompetitor)
    except Exception:                                   # pragma: no cover
        return []
    ids = [l.id for l in leads]
    if not ids:
        return []
    stage_by_lead = {l.id: (l.stage or '') for l in leads}
    names = {c.id: c.name for c in CompetitorMaster.query.with_entities(
        CompetitorMaster.id, CompetitorMaster.name).all()}
    by_comp = {}
    for i in range(0, len(ids), 500):
        rows = (OpportunityCompetitor.query
                .filter(OpportunityCompetitor.lead_id.in_(ids[i:i + 500]),
                        OpportunityCompetitor.is_active.isnot(False)).all())
        for r in rows:
            name = names.get(r.competitor_id) or f'Competitor #{r.competitor_id}'
            cell = by_comp.setdefault(name, {
                'competitor': name, 'competitor_id': r.competitor_id,
                'encounters': 0, 'won': 0, 'lost': 0,
                'route': f'/competitors/{r.competitor_id}'})
            cell['encounters'] += 1
            stage = stage_by_lead.get(r.lead_id, '')
            if stage == 'Won':
                cell['won'] += 1
            elif stage == 'Lost':
                cell['lost'] += 1
    return sorted(by_comp.values(), key=lambda r: -r['encounters'])


def _top_accounts(leads):
    """The accounts carrying the most, by what their open deals are worth."""
    by_account = {}
    for l in leads:
        if not l.company_id and not l.company:
            continue
        key = l.company_id or l.company
        cell = by_account.setdefault(key, {
            'account_id': l.company_id, 'name': l.company or '',
            'open': 0, 'open_value_inr': 0.0, 'won': 0, 'won_value_inr': 0.0,
            'route': (f'/companies/{l.company_id}' if l.company_id else '')})
        value = _value(l)
        if (l.stage or '') in rules.open_stages():
            cell['open'] += 1
            cell['open_value_inr'] = round(cell['open_value_inr'] + value, 2)
        elif l.stage == 'Won':
            cell['won'] += 1
            cell['won_value_inr'] = round(cell['won_value_inr'] + value, 2)
    rows = sorted(by_account.values(),
                  key=lambda r: -(r['open_value_inr'] + r['won_value_inr']))
    return rows[:LIST_LIMIT]


def _visits(codes, start, end):
    """Customer visits and meetings logged in the period, by person."""
    from app import LeadActivity
    lo = datetime.combine(start, datetime.min.time())
    hi = datetime.combine(end, datetime.max.time())
    by_person = {}
    if codes:
        rows = (LeadActivity.query
                .with_entities(LeadActivity.performed_by,
                               LeadActivity.lead_id)
                .filter(LeadActivity.performed_by.in_(codes or {''}),
                        LeadActivity.kind.in_(VISIT_KINDS),
                        LeadActivity.occurred_at >= lo,
                        LeadActivity.occurred_at <= hi).all())
        for who, lead_id in rows:
            cell = by_person.setdefault(who or '', {'emp_code': who or '',
                                                    'visits': 0,
                                                    'accounts': set()})
            cell['visits'] += 1
            if lead_id:
                cell['accounts'].add(lead_id)
    out = []
    for cell in by_person.values():
        out.append({'emp_code': cell['emp_code'], 'visits': cell['visits'],
                    'leads': len(cell['accounts']),
                    'route': f"/review/individual?emp={cell['emp_code']}"})
    out.sort(key=lambda r: -r['visits'])
    return {'total': sum(r['visits'] for r in out), 'by_person': out,
            'kinds': list(VISIT_KINDS)}


def _cross_sell(sc, vertical_name):
    """Accounts this desk works that another desk also works.

    The whole point of a vertical review asking the question: a customer
    buying one service from Procam and another from somebody else is the
    cheapest pipeline there is.
    """
    from app import Lead
    rows = sc_mod.leads(Lead.query.filter(
        Lead.is_archived.isnot(True),
        Lead.company_id.isnot(None)), sc=sc).with_entities(
        Lead.company_id, Lead.company, Lead.procam_vertical).all()
    mine = {r[0] for r in rows
            if (r[2] or '').strip().lower() == vertical_name.strip().lower()}
    if not mine:
        return []
    others = {}
    for company_id, name, vert in rows:
        if company_id not in mine:
            continue
        vert = (vert or '').strip()
        if not vert or vert.lower() == vertical_name.strip().lower():
            continue
        cell = others.setdefault(company_id, {
            'account_id': company_id, 'name': name or '',
            'verticals': set(), 'route': f'/companies/{company_id}'})
        cell['verticals'].add(vert)
    out = [{'account_id': c['account_id'], 'name': c['name'],
            'verticals': sorted(c['verticals']), 'route': c['route']}
           for c in others.values()]
    out.sort(key=lambda r: (-len(r['verticals']), r['name']))
    return out[:LIST_LIMIT]


# ── the meeting ──────────────────────────────────────────────────────
#: The agenda, in the order the meeting runs it. Fixed on purpose: a
#: review that starts with the wins and never reaches the stale list is
#: the review everybody enjoys and nobody learns from.
AGENDA = (
    ('previous_actions', 'Previous actions'),
    ('new_leads', 'New leads'),
    ('rfqs', 'RFQs'),
    ('quotes_pending', 'Quotes pending'),
    ('negotiations', 'Negotiations'),
    ('expected_closures', 'Expected closures'),
    ('won', 'Won'),
    ('lost', 'Lost'),
    ('stale', 'Stale'),
    ('account_development', 'Account development'),
    ('external_intelligence', 'External intelligence'),
    ('actions_agreed', 'Actions agreed'),
)
AGENDA_KEYS = tuple(k for k, _t in AGENDA)

#: How far ahead "expected closures" looks when the lead carries no
#: expected close date of its own.
CLOSURE_HORIZON_DAYS = 30


def meeting(scope_key, period='week', sc=None, start=None, end=None):
    """The agenda for one review meeting, in the order it is run."""
    sc = _viewer(sc)
    kind, value, msc = require_scope_key(scope_key, sc)
    start, end = period_dates(start, end, period)
    today = rules.business_today()
    scope_key = f'{kind}:{value}'

    leads = _lead_rows(msc)
    open_stages = rules.open_stages()
    idle_map = rules.idle_days_map(
        [l.id for l in leads if (l.stage or '') in open_stages])

    new_leads, negotiations, expected, won, lost, stale = [], [], [], [], [], []
    horizon = today + timedelta(days=CLOSURE_HORIZON_DAYS)
    for l in leads:
        row = _lead_row(l)
        if _within(l.created_at, start, end):
            new_leads.append(row)
        closed = _closed_on(l)
        if l.stage == 'Won' and _within(closed, start, end):
            won.append(row)
        if l.stage == 'Lost' and _within(closed, start, end):
            lost.append(row)
        if (l.stage or '') not in open_stages:
            continue
        quiet = rules.idle_days(l, idle_map.get(l.id))
        if l.stage == 'Under Negotiation':
            negotiations.append(_lead_row(l, quiet_days=quiet))
        due = l.opp_close_date or l.followup_date
        if due is not None and due <= horizon:
            expected.append(_lead_row(l, quiet_days=quiet))
        if quiet is not None and quiet >= rules.STALE_DAYS:
            stale.append(_lead_row(l, quiet_days=quiet))

    ageing = rules.rfq_ageing(sc=msc, today=today)
    pending = sorted([r for rs in ageing['buckets'].values() for r in rs],
                     key=lambda r: -(r['age_days'] or 0))
    for row in pending:
        row['route'] = (f"/app?lead={row['lead_id']}" if row.get('lead_id')
                        else f"/rfqs/{row['rfq_id']}")
    commercial = _commercial(msc, leads, start, end)
    accounts = _accounts(msc, start, end, today)

    sections = {
        'previous_actions': _section(
            'previous_actions', 'Previous actions',
            [a.to_dict(today) for a in _unresolved(scope_key)],
            note='Carried forward until they are resolved.'),
        'new_leads': _section('new_leads', 'New leads',
                              sorted(new_leads,
                                     key=lambda r: -r['value_inr'])),
        'rfqs': _section('rfqs', 'RFQs', commercial['rfq_rows'],
                         note=f"{commercial['rfqs_on_leads']} leads were "
                              f"marked RFQ-received in the period."),
        'quotes_pending': _section(
            'quotes_pending', 'Quotes pending', pending,
            note=f"{ageing['overdue_count']} past the date we committed to."),
        'negotiations': _section('negotiations', 'Negotiations',
                                 sorted(negotiations,
                                        key=lambda r: -r['value_inr'])),
        'expected_closures': _section(
            'expected_closures', 'Expected closures',
            sorted(expected, key=lambda r: r['expected_close']
                   or r['next_action_date'] or '9999-12-31'),
            note=f'Dated within {CLOSURE_HORIZON_DAYS} days.'),
        'won': _section('won', 'Won',
                        sorted(won, key=lambda r: -r['value_inr'])),
        'lost': _section('lost', 'Lost',
                         sorted(lost, key=lambda r: -r['value_inr'])),
        'stale': _section('stale', 'Stale',
                          sorted(stale,
                                 key=lambda r: -(r['quiet_days'] or 0)),
                          note=f'No contact for {rules.STALE_DAYS} days '
                               f'or more.'),
        'account_development': _section(
            'account_development', 'Account development',
            accounts['action_due'] + accounts['quiet'],
            note=f"{accounts['new_count']} accounts opened in the period."),
        'external_intelligence': _section(
            'external_intelligence', 'External intelligence',
            _external_intelligence(msc, start, end),
            note='News and competitor notes recorded in the period.'),
        'actions_agreed': _section(
            'actions_agreed', 'Actions agreed',
            [a.to_dict(today) for a in _agreed(scope_key, start, end)],
            note='Written down here, so the next meeting opens with them.'),
    }
    return {
        'review_scope': scope_key,
        'subject': {'kind': kind, 'value': value},
        'period': {'name': period, 'start': str(start), 'end': str(end),
                   'days': (end - start).days + 1},
        'agenda': [sections[key] for key, _title in AGENDA],
        'sections': sections,
        'attention': _attention(msc),
    }


def _external_intelligence(sc, start, end):
    """Market news and competitor notes. Empty when nobody feeds them —
    an empty section is the honest answer, not a missing one."""
    rows = []
    try:
        from app import NewsItem
        news = (NewsItem.query
                .filter(NewsItem.status != 'deleted')
                .order_by(NewsItem.id.desc()).limit(200).all())
        for n in news:
            when = n.published_date or _as_date(n.created_at)
            if when is None or not (start <= when <= end):
                continue
            rows.append({'kind': 'news', 'title': n.title or '',
                         'detail': (n.summary or '')[:200],
                         'source': n.source or '', 'date': str(when),
                         'route': n.url or '/triage'})
    except Exception:                                   # pragma: no cover
        from app import db
        db.session.rollback()
    try:
        from app.models.competitor import CompetitorIntelligence
        notes = (CompetitorIntelligence.query
                 .filter(CompetitorIntelligence.event_date >= start,
                         CompetitorIntelligence.event_date <= end)
                 .order_by(CompetitorIntelligence.event_date.desc())
                 .limit(100).all())
        for n in notes:
            target = n.company_id or n.competitor_id
            rows.append({
                'kind': 'competitor',
                'title': (n.event_type or 'Competitor note').title(),
                'detail': (n.summary or '')[:200],
                'source': n.source or '',
                'date': str(n.event_date),
                'route': (f'/competitors/{target}' if target
                          else '/competitors')})
    except Exception:
        from app import db
        db.session.rollback()
    rows.sort(key=lambda r: r['date'], reverse=True)
    return rows


# ── review actions ───────────────────────────────────────────────────
def _unresolved(scope_key):
    """Everything still owed on this review scope, oldest due first.

    This is the whole carry-forward mechanism: nothing marks an action as
    "for the next meeting", because an action that is not resolved simply
    never stops being listed.
    """
    return (ReviewAction.query
            .filter(ReviewAction.review_scope == scope_key,
                    ReviewAction.status.in_(ReviewActionStatus.UNRESOLVED))
            .order_by(ReviewAction.due_date.asc().nullslast(),
                      ReviewAction.id.asc()).all())


def _agreed(scope_key, start, end):
    """Actions written down during this period's meeting."""
    lo = datetime.combine(start, datetime.min.time())
    hi = datetime.combine(end, datetime.max.time())
    return (ReviewAction.query
            .filter(ReviewAction.review_scope == scope_key,
                    ReviewAction.created_at >= lo,
                    ReviewAction.created_at <= hi)
            .order_by(ReviewAction.id.asc()).all())


def actions(scope_key, sc=None, status='', owner='', subject=''):
    """The actions on one review scope, as dictionaries.

    `status` takes a single status, 'unresolved', or '' for everything.
    """
    sc = _viewer(sc)
    kind, value, _msc = require_scope_key(scope_key, sc)
    scope_key = f'{kind}:{value}'
    q = ReviewAction.query.filter(ReviewAction.review_scope == scope_key)
    if status == 'unresolved':
        q = q.filter(ReviewAction.status.in_(ReviewActionStatus.UNRESOLVED))
    elif status:
        q = q.filter(ReviewAction.status == status)
    if owner:
        q = q.filter(ReviewAction.owner_emp_code == owner.upper())
    if subject:
        q = q.filter(ReviewAction.subject_emp_code == subject.upper())
    today = rules.business_today()
    rows = q.order_by(ReviewAction.status.asc(),
                      ReviewAction.due_date.asc().nullslast(),
                      ReviewAction.id.asc()).limit(500).all()
    return [a.to_dict(today) for a in rows]


def create_action(scope_key, description, *, owner_emp_code='',
                  subject_emp_code='', due_date=None, linked_entity_type='',
                  linked_entity_id=None, actor='', sc=None,
                  carried_from=None):
    """Write down what was agreed. Refuses what the viewer may not reach."""
    from app import db
    from app.models.review import LINKED_TYPES
    from app.services import audit

    sc = _viewer(sc)
    kind, value, _msc = require_scope_key(scope_key, sc)
    scope_key = f'{kind}:{value}'
    description = (description or '').strip()
    if not description:
        raise ReviewRefused('Say what was agreed', status=400)
    if len(description) > 500:
        raise ReviewRefused('Keep the action under 500 characters',
                            status=400)

    owner = (owner_emp_code or '').strip().upper()
    subject = (subject_emp_code or '').strip().upper()
    if kind == 'emp':
        subject = subject or value
    owner = owner or subject or (sc.emp_code or '')
    # An action can only be given to somebody the viewer may already see.
    for code in {c for c in (owner, subject) if c}:
        if not may_review_person(code, sc):
            raise ReviewRefused('That person is outside your access')

    link_type = (linked_entity_type or '').strip().lower()
    if link_type not in LINKED_TYPES:
        raise ReviewRefused('That is not a record an action can point at',
                            status=400)
    try:
        link_id = int(linked_entity_id) if linked_entity_id else None
    except (TypeError, ValueError):
        raise ReviewRefused('The linked record must be an id', status=400)
    if link_type and link_id:
        _require_linked_record(link_type, link_id, sc)

    due = _as_date(due_date)
    if due_date and due is None:
        raise ReviewRefused('The due date must be YYYY-MM-DD', status=400)

    action = ReviewAction(
        review_scope=scope_key, subject_emp_code=subject or None,
        owner_emp_code=owner or None, due_date=due, description=description,
        linked_entity_type=link_type or None, linked_entity_id=link_id,
        status=ReviewActionStatus.OPEN,
        created_by=(actor or sc.emp_code or '')[:20],
        created_at=datetime.utcnow(),
        carried_from_id=getattr(carried_from, 'id', None) or carried_from)
    db.session.add(action)
    db.session.flush()
    audit.record('review.action.create', 'review_action', action.id,
                 new=action.to_dict(), actor=actor or sc.emp_code,
                 reason=description[:200])
    db.session.commit()
    return action


def _require_linked_record(link_type, link_id, sc):
    """An action may only point at a record the viewer may open."""
    from app import Company, Lead
    if link_type == 'lead':
        row = sc_mod.leads(Lead.query.filter(Lead.id == link_id),
                           sc=sc).first()
    elif link_type == 'account':
        row = sc_mod.companies(Company.query.filter(Company.id == link_id),
                               sc=sc).first()
    elif link_type == 'rfq':
        from app.models.rfq import RFQ
        row = sc_mod.rfqs(RFQ.query.filter(RFQ.id == link_id), sc=sc).first()
    elif link_type == 'quote':
        from app.models.quote import Quote
        row = sc_mod.quotes(Quote.query.filter(Quote.id == link_id),
                            sc=sc).first()
    else:
        return
    if row is None:
        raise ReviewRefused('That record is outside your access')


def close_action(action_id, *, outcome=ReviewActionStatus.DONE, note='',
                 due_date=None, actor='', sc=None):
    """Resolve an action: done, or carried forward with a new date.

    Carrying forward keeps the original row closed and opens a new one
    pointing back at it, so a commitment that has slipped three times
    reads as three rows rather than one date that kept moving.
    """
    from app import db
    from app.services import audit

    sc = _viewer(sc)
    action = db.session.get(ReviewAction, int(action_id))
    if action is None:
        raise ReviewRefused('No such review action', status=404)
    # The scope key decides who may touch it — the same check that
    # decided who could see the review it came from.
    require_scope_key(action.review_scope, sc)
    if action.status != ReviewActionStatus.OPEN:
        raise ReviewRefused('That action is already closed', status=409)
    if outcome not in (ReviewActionStatus.DONE, ReviewActionStatus.CARRIED):
        raise ReviewRefused('An action is either done or carried forward',
                            status=400)

    before = action.to_dict()
    action.status = outcome
    action.closed_at = datetime.utcnow()
    successor = None
    db.session.flush()
    audit.record('review.action.close', 'review_action', action.id,
                 old=before, new=action.to_dict(),
                 actor=actor or sc.emp_code, reason=(note or outcome)[:200])
    db.session.commit()

    if outcome == ReviewActionStatus.CARRIED:
        successor = create_action(
            action.review_scope, action.description,
            owner_emp_code=action.owner_emp_code or '',
            subject_emp_code=action.subject_emp_code or '',
            due_date=due_date or action.due_date,
            linked_entity_type=action.linked_entity_type or '',
            linked_entity_id=action.linked_entity_id,
            actor=actor, sc=sc, carried_from=action.id)
    return action, successor


# ── management command view ──────────────────────────────────────────
#: The tiles of the command view, in the order the brief lists them. Each
#: is a count the reader can open — a figure with no records behind it is
#: a complaint, not a tool.
COMMAND_TILES = (
    ('actions_today', 'Actions due today'),
    ('delayed_quotes', 'Delayed quotes'),
    ('stuck_large', 'Stuck large opportunities'),
    ('ageing_pipeline', 'Ageing pipeline by vertical'),
    ('inactive_customers', 'Inactive customers'),
    ('overdue_actions', 'Overdue CRM actions'),
    ('loss_reasons', 'Loss reasons'),
    ('expected_closures', 'Expected closures'),
    ('new_entries', 'New funnel entries'),
)


def command_view(sc=None, start=None, end=None):
    """What a manager has to act on across everything they may see.

    Every figure carries its rows, because the question a management
    view provokes is always "which ones?".
    """
    sc = _viewer(sc)
    start, end = period_dates(start, end, 'month')
    today = rules.business_today()
    leads = _lead_rows(sc)
    open_stages = rules.open_stages()
    open_leads = [l for l in leads if (l.stage or '') in open_stages]
    idle_map = rules.idle_days_map([l.id for l in open_leads])

    actions_today, overdue_actions, stuck, expected, new_entries = \
        [], [], [], [], []
    by_vertical = {}
    horizon = today + timedelta(days=CLOSURE_HORIZON_DAYS)
    for l in open_leads:
        quiet = rules.idle_days(l, idle_map.get(l.id))
        row = _lead_row(l, quiet_days=quiet)
        if l.followup_date == today:
            actions_today.append(row)
        elif l.followup_date and l.followup_date < today:
            overdue_actions.append(row)
        if rules.is_high_value(l) and quiet is not None \
                and quiet >= rules.STALE_DAYS:
            stuck.append(row)
        due = l.opp_close_date or l.followup_date
        if due is not None and due <= horizon:
            expected.append(row)
        key = l.procam_vertical or 'Not set'
        cell = by_vertical.setdefault(key, {
            'vertical': key, 'count': 0, 'value_inr': 0.0,
            'over_stale': 0, 'average_age_days': 0.0, '_ages': [],
            'route': f'/review/vertical?vertical={key}'})
        cell['count'] += 1
        cell['value_inr'] = round(cell['value_inr'] + row['value_inr'], 2)
        age = row['age_days'] or 0
        cell['_ages'].append(age)
        if age >= rules.STALE_DAYS:
            cell['over_stale'] += 1
    for l in leads:
        if _within(l.created_at, start, end):
            new_entries.append(_lead_row(l))
    ageing_rows = []
    for cell in by_vertical.values():
        ages = cell.pop('_ages')
        cell['average_age_days'] = round(sum(ages) / len(ages), 1) if ages else 0
        ageing_rows.append(cell)
    ageing_rows.sort(key=lambda r: -r['value_inr'])

    rfq_ageing = rules.rfq_ageing(sc=sc, today=today)
    delayed = sorted(rfq_ageing['overdue'],
                     key=lambda r: -(r['days_late'] or 0))
    for row in delayed:
        row['route'] = (f"/app?lead={row['lead_id']}" if row.get('lead_id')
                        else f"/rfqs/{row['rfq_id']}")
    accounts = _accounts(sc, start, end, today)
    lost_rows = [_lead_row(l) for l in leads
                 if l.stage == 'Lost' and _within(_closed_on(l), start, end)]

    tiles = {
        'actions_today': _section('actions_today', 'Actions due today',
                                  actions_today),
        'delayed_quotes': _section('delayed_quotes', 'Delayed quotes',
                                   delayed,
                                   note='Past the date we committed to.'),
        'stuck_large': _section(
            'stuck_large', 'Stuck large opportunities', stuck,
            note=f'Worth {lead_value.format_inr(rules.HIGH_VALUE_INR)} or '
                 f'more and quiet for {rules.STALE_DAYS} days.'),
        'inactive_customers': _section(
            'inactive_customers', 'Inactive customers', accounts['quiet'],
            note=f"No activity for {rules.ACCOUNT_QUIET_DAYS} days."),
        'overdue_actions': _section('overdue_actions', 'Overdue CRM actions',
                                    overdue_actions),
        'expected_closures': _section('expected_closures',
                                      'Expected closures', expected),
        'new_entries': _section('new_entries', 'New funnel entries',
                                new_entries),
    }
    return {
        'period': {'start': str(start), 'end': str(end)},
        'tiles': tiles,
        'ageing_pipeline': ageing_rows,
        'loss_reasons': _loss_reasons(lost_rows),
        'review_actions': {
            'open': _open_actions_for_viewer(sc, today),
        },
        'attention': _attention(sc),
        'order': [k for k, _t in COMMAND_TILES],
        'titles': dict(COMMAND_TILES),
    }


def _open_actions_for_viewer(sc, today):
    """Review actions still owed by anybody the viewer reaches."""
    q = ReviewAction.query.filter(
        ReviewAction.status.in_(ReviewActionStatus.UNRESOLVED))
    if not sc.unrestricted:
        codes = set(sc.codes or ()) or {''}
        q = q.filter(or_(ReviewAction.owner_emp_code.in_(codes),
                         ReviewAction.subject_emp_code.in_(codes)))
    rows = q.order_by(ReviewAction.due_date.asc().nullslast()).limit(200).all()
    return [a.to_dict(today) for a in rows]
