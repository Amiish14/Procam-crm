"""
The approved queries — one per intent, every one scoped.

Rules that hold for every handler in this file, without exception:

  * It receives an already-resolved Scope and passes it to the query
    helpers. It never calls scope.current() itself, so a caller can run
    any intent as any persona — which is what makes the §16 RBAC suite
    possible.
  * It returns a Result. Numbers are computed here and rendered by the
    UI; the narrator is handed the Result and may not invent a figure.
  * Absent data returns `empty=True` and says so. It never returns a
    plausible number, because a confident wrong figure is the most
    damaging thing this system can do.
  * Row limits everywhere. A question that matches ten thousand rows
    answers with the top slice and says how many there were.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, or_

from app.access import scope as sc_mod
from app.copilot.intents import Result, intent


#: No answer ever returns more than this many rows to the panel.
ROW_CAP = 50

#: "Stale" and "idle" mean this unless the question says otherwise.
DEFAULT_IDLE_DAYS = 7

_TERMINAL = ('Won', 'Lost', 'On Hold', 'Not Interested')


def _now():
    return datetime.utcnow()


def _days_ago(n):
    return _now() - timedelta(days=int(n or 0))


def _cap(rows, total=None):
    """Trim to the cap and say what was trimmed."""
    total = len(rows) if total is None else total
    if total > ROW_CAP:
        return rows[:ROW_CAP], [f'Showing the first {ROW_CAP} of {total}.']
    return rows[:ROW_CAP], []


def _money(v):
    """Indian scales, because a logistics quote is read in lakh and
    crore and '₹4,80,00,000' is not read at all."""
    try:
        n = float(v or 0)
    except (TypeError, ValueError):
        return '—'
    if n >= 1e7:
        return f'₹{n / 1e7:.2f} cr'
    if n >= 1e5:
        return f'₹{n / 1e5:.2f} L'
    return f'₹{n:,.0f}'


def _age(dt):
    if not dt:
        return None
    if isinstance(dt, datetime):
        return (_now() - dt).days
    try:
        return (_now().date() - dt).days
    except Exception:
        return None


def _lead_chip(lead):
    return {'type': 'lead', 'id': lead.id, 'label': lead.company or 'Lead'}


# ══════════════════════════════════════════════════════════════════════
#  MY DAY  ·  §5 daily sales
# ══════════════════════════════════════════════════════════════════════
@intent('my_day', 'My Day',
        personas=('sales', 'head'), phase=1,
        examples=('what needs my attention today',
                  'my day', 'what should I work on',
                  "what's pending for me"))
def my_day(scope, params):
    """§6.5 — the morning brief, from the tiles that already exist.

    Deliberately recomputed from the same columns the My Work tiles use
    rather than from a second definition of "overdue", so the Copilot
    and the tile can never disagree.
    """
    from app import Lead

    today = _now().date()
    mine = sc_mod.leads(sc=scope).filter(~Lead.stage.in_(_TERMINAL))

    overdue = mine.filter(Lead.followup_date.isnot(None),
                          Lead.followup_date < today).count()
    due_today = mine.filter(Lead.followup_date == today).count()
    idle = mine.filter(or_(Lead.updated_at.is_(None),
                           Lead.updated_at < _days_ago(3))).count()
    awaiting_quote = mine.filter(Lead.stage == 'RFQ Generated').count()
    negotiating = mine.filter(Lead.stage == 'Under Negotiation').count()
    new_this_week = mine.filter(Lead.created_at >= _days_ago(7)).count()

    figures = {
        'overdue': overdue, 'due_today': due_today, 'idle_3_days': idle,
        'awaiting_quote': awaiting_quote, 'in_negotiation': negotiating,
        'new_this_week': new_this_week,
    }
    total = sum(figures.values())
    if not total:
        return Result(headline='Nothing is waiting on you right now.',
                      figures=figures, empty=True,
                      sources=['My Work tiles'])

    return Result(
        headline=(f'{overdue} overdue · {due_today} due today · '
                  f'{idle} idle 3+ days · {awaiting_quote} awaiting a quote'),
        figures=figures,
        sources=['My Work tiles', 'leads.followup_date', 'leads.stage'])


# ══════════════════════════════════════════════════════════════════════
#  LEADS
# ══════════════════════════════════════════════════════════════════════
@intent('leads_stale', 'Stale leads',
        params={'days': 'how many days without activity (default 7)'},
        personas=('sales', 'head'), phase=1,
        examples=('leads with no activity for 7 days',
                  'which of my leads have gone quiet',
                  'stale leads', 'untouched leads'))
def leads_stale(scope, params):
    from app import Lead, LeadActivity

    days = int(params.get('days') or DEFAULT_IDLE_DAYS)
    cutoff = _days_ago(days)

    last_activity = (
        LeadActivity.query
        .with_entities(LeadActivity.lead_id,
                       func.max(LeadActivity.occurred_at).label('last'))
        .group_by(LeadActivity.lead_id).subquery())

    q = (sc_mod.leads(sc=scope)
         .outerjoin(last_activity, last_activity.c.lead_id == Lead.id)
         .filter(~Lead.stage.in_(_TERMINAL))
         .filter(or_(last_activity.c.last.is_(None),
                     last_activity.c.last < cutoff))
         .order_by(Lead.created_at.asc()))

    found = q.all()
    if not found:
        return Result(headline=f'No leads have been idle for {days}+ days.',
                      empty=True, sources=['lead_activities.occurred_at'])

    rows = [{'Company': l.company, 'Stage': l.stage,
             'Value': _money(l.estimated_value_inr),
             'Idle days': _age(l.updated_at or l.created_at) or 0,
             'Owner': l.assigned_to or '—', '_chip': _lead_chip(l)}
            for l in found]
    rows, notes = _cap(rows, len(found))
    return Result(
        headline=f'{len(found)} lead(s) with no activity for {days}+ days.',
        columns=['Company', 'Stage', 'Value', 'Idle days', 'Owner'],
        rows=rows, figures={'count': len(found), 'days': days}, notes=notes,
        sources=['leads.stage', 'lead_activities.occurred_at'])


@intent('leads_open', 'Open leads',
        personas=('sales', 'head'), phase=1,
        examples=('my open leads', 'what leads are open',
                  'show me active leads'))
def leads_open(scope, params):
    from app import Lead

    found = (sc_mod.leads(sc=scope).filter(~Lead.stage.in_(_TERMINAL))
             .order_by(Lead.created_at.desc()).all())
    if not found:
        return Result(headline='No open leads.', empty=True,
                      sources=['leads.stage'])
    rows = [{'Company': l.company, 'Stage': l.stage,
             'Value': _money(l.estimated_value_inr),
             'Owner': l.assigned_to or '—',
             'Age': _age(l.created_at) or 0, '_chip': _lead_chip(l)}
            for l in found]
    rows, notes = _cap(rows, len(found))
    return Result(headline=f'{len(found)} open lead(s).',
                  columns=['Company', 'Stage', 'Value', 'Owner', 'Age'],
                  rows=rows, figures={'count': len(found)}, notes=notes,
                  sources=['leads.stage'])


@intent('leads_no_next_action', 'Leads with no next action',
        personas=('sales', 'head', 'admin'), phase=1,
        examples=('leads with no follow-up date',
                  'which leads have no next action'))
def leads_no_next_action(scope, params):
    from app import Lead

    found = (sc_mod.leads(sc=scope)
             .filter(~Lead.stage.in_(_TERMINAL))
             .filter(Lead.followup_date.is_(None))
             .order_by(Lead.created_at.desc()).all())
    if not found:
        return Result(headline='Every open lead has a next action set.',
                      empty=True, sources=['leads.followup_date'])
    rows = [{'Company': l.company, 'Stage': l.stage,
             'Owner': l.assigned_to or '—',
             'Value': _money(l.estimated_value_inr), '_chip': _lead_chip(l)}
            for l in found]
    rows, notes = _cap(rows, len(found))
    return Result(
        headline=f'{len(found)} open lead(s) with no next action recorded.',
        columns=['Company', 'Stage', 'Owner', 'Value'], rows=rows,
        figures={'count': len(found)}, notes=notes,
        sources=['leads.followup_date'])


# ══════════════════════════════════════════════════════════════════════
#  RFQs AND QUOTES
# ══════════════════════════════════════════════════════════════════════
@intent('rfqs_unquoted', 'RFQs not yet quoted',
        permission='module.rfq', personas=('sales', 'head'), phase=1,
        examples=('which RFQs have I not quoted',
                  'RFQs pending quotation', 'unquoted RFQs'))
def rfqs_unquoted(scope, params):
    from app.models.quote import Quote
    from app.models.rfq import RFQ

    quoted_ids = {q.rfq_id for q in
                  Quote.query.with_entities(Quote.rfq_id)
                  .filter(Quote.rfq_id.isnot(None)).all()}
    found = [r for r in sc_mod.rfqs(sc=scope)
             .order_by(RFQ.received_date.asc()).all()
             if r.id not in quoted_ids]

    if not found:
        return Result(headline='Every RFQ in your scope has been quoted.',
                      empty=True, sources=['rfqs', 'quotes.rfq_id'])

    today = _now().date()
    rows = []
    overdue = 0
    for r in found:
        late = bool(r.quote_by_date and r.quote_by_date < today)
        overdue += late
        rows.append({'RFQ': r.rfq_number, 'Subject': r.subject,
                     'Received': str(r.received_date or '')[:10],
                     'Quote by': str(r.quote_by_date or '')[:10] or '—',
                     'Overdue': 'yes' if late else '',
                     'Owner': r.lead_driver or '—',
                     '_chip': {'type': 'rfq', 'id': r.id,
                               'label': r.rfq_number}})
    rows.sort(key=lambda x: (x['Overdue'] != 'yes', x['Received']))
    rows, notes = _cap(rows, len(found))
    return Result(
        headline=(f'{len(found)} RFQ(s) not yet quoted'
                  + (f', {overdue} past their quote-by date.' if overdue
                     else '.')),
        columns=['RFQ', 'Subject', 'Received', 'Quote by', 'Overdue',
                 'Owner'],
        rows=rows, figures={'count': len(found), 'overdue': overdue},
        notes=notes, sources=['rfqs.quote_by_date', 'quotes.rfq_id'])


@intent('quotes_awaiting_reply', 'Quotes awaiting a reply',
        permission='module.quotes',
        params={'days': 'minimum days since the quote was sent'},
        personas=('sales', 'head'), phase=1,
        examples=('quotes with no follow-up', 'quotes waiting for a reply',
                  'which quotes has the customer not answered'))
def quotes_awaiting_reply(scope, params):
    from app import LeadEmail
    from app.models.quote import Quote

    days = int(params.get('days') or 0)
    q = sc_mod.quotes(sc=scope).filter(Quote.status == 'Submitted')
    if days:
        q = q.filter(Quote.quote_date <= _days_ago(days).date())
    found = q.order_by(Quote.quote_date.asc()).all()
    if not found:
        return Result(headline='No submitted quotes are waiting on a reply.',
                      empty=True, sources=['quotes.status'])

    rows = []
    for qt in found:
        replied = False
        if qt.lead_id and qt.quote_date:
            replied = bool(
                LeadEmail.query
                .filter(LeadEmail.lead_id == qt.lead_id,
                        LeadEmail.direction == 'inbound',
                        LeadEmail.sent_or_received_at >= qt.quote_date)
                .first())
        if replied:
            continue
        rows.append({'Quote': qt.quote_number, 'Subject': qt.subject or '—',
                     'Value': _money(qt.total_amount),
                     'Sent': str(qt.quote_date or '')[:10],
                     'Days waiting': _age(qt.quote_date) or 0,
                     'Prepared by': qt.prepared_by_id or '—',
                     '_chip': {'type': 'quote', 'id': qt.id,
                               'label': qt.quote_number}})
    if not rows:
        return Result(
            headline='Every submitted quote has had a reply.',
            empty=True, sources=['quotes.status', 'lead_emails.direction'])
    total = len(rows)
    rows, notes = _cap(rows, total)
    return Result(
        headline=f'{total} quote(s) submitted with no customer reply since.',
        columns=['Quote', 'Subject', 'Value', 'Sent', 'Days waiting',
                 'Prepared by'],
        rows=rows, figures={'count': total}, notes=notes,
        sources=['quotes.status', 'quotes.quote_date',
                 'lead_emails.sent_or_received_at'])


@intent('quotes_above', 'Quotes above a value',
        permission='module.quotes',
        params={'amount': 'the threshold in rupees'},
        personas=('head', 'mgmt'), phase=1,
        examples=('quotes above 50 lakh', 'quotes over 1 crore',
                  'big quotes this month'))
def quotes_above(scope, params):
    from app.models.quote import Quote

    try:
        threshold = float(params.get('amount') or 0)
    except (TypeError, ValueError):
        threshold = 0
    if threshold <= 0:
        return Result(headline='How large? Give me a figure — "quotes above '
                               '50 lakh", for instance.', empty=True)

    found = (sc_mod.quotes(sc=scope)
             .filter(Quote.total_amount >= threshold)
             .order_by(Quote.total_amount.desc()).all())
    if not found:
        return Result(headline=f'No quotes above {_money(threshold)}.',
                      empty=True, sources=['quotes.total_amount'])
    rows = [{'Quote': q.quote_number, 'Value': _money(q.total_amount),
             'Status': q.status, 'Date': str(q.quote_date or '')[:10],
             'Prepared by': q.prepared_by_id or '—',
             '_chip': {'type': 'quote', 'id': q.id,
                       'label': q.quote_number}} for q in found]
    rows, notes = _cap(rows, len(found))
    return Result(headline=f'{len(found)} quote(s) above {_money(threshold)}.',
                  columns=['Quote', 'Value', 'Status', 'Date',
                           'Prepared by'],
                  rows=rows, figures={'count': len(found),
                                      'threshold': threshold},
                  notes=notes, sources=['quotes.total_amount'])


@intent('last_quote_for_account', 'Last quote to an account',
        permission='module.quotes',
        params={'account': 'the customer name'},
        personas=('sales', 'head'), phase=1,
        examples=('what did we last quote Tata Steel',
                  'last price we gave JSW', 'our most recent quote to BEML'))
def last_quote_for_account(scope, params):
    from app import Company
    from app.models.quote import Quote

    name = (params.get('account') or '').strip()
    if not name:
        return Result(headline='Which customer?', empty=True)

    company = _resolve_company(name)
    if company is None:
        return Result(headline=f'No account matching "{name}" in the CRM.',
                      empty=True, sources=['companies.name'])

    qt = (sc_mod.quotes(sc=scope).filter(Quote.account_id == company.id)
          .order_by(Quote.quote_date.desc()).first())
    if qt is None:
        return Result(
            headline=(f'No quote to {company.name} that you have access to.'),
            empty=True, sources=['quotes.account_id'])
    return Result(
        headline=(f'{company.name} — last quote {qt.quote_number}, '
                  f'{_money(qt.total_amount)} on '
                  f'{str(qt.quote_date or "")[:10]} ({qt.status}).'),
        figures={'amount': float(qt.total_amount or 0),
                 'quote_number': qt.quote_number},
        rows=[{'Quote': qt.quote_number, 'Value': _money(qt.total_amount),
               'Date': str(qt.quote_date or '')[:10], 'Status': qt.status,
               '_chip': {'type': 'quote', 'id': qt.id,
                         'label': qt.quote_number}}],
        columns=['Quote', 'Value', 'Date', 'Status'],
        sources=[f'Quote {qt.quote_number}', 'companies.name'])


@intent('quote_turnaround', 'Quote turnaround time',
        permission='module.quotes', personas=('head', 'mgmt'), phase=2,
        examples=('how long do we take to quote',
                  'quotation turnaround time', 'RFQ to quote time'))
def quote_turnaround(scope, params):
    from app.models.quote import Quote
    from app.models.rfq import RFQ

    pairs = []
    for qt in sc_mod.quotes(sc=scope).filter(Quote.rfq_id.isnot(None)).all():
        rfq = RFQ.query.get(qt.rfq_id)
        if rfq and rfq.received_date and qt.quote_date:
            pairs.append((qt.quote_date - rfq.received_date).days)
    if not pairs:
        return Result(
            headline='No quotes linked to an RFQ with both dates recorded, '
                     'so turnaround cannot be measured yet.',
            empty=True, sources=['rfqs.received_date', 'quotes.quote_date'])

    pairs.sort()
    median = pairs[len(pairs) // 2]
    p90 = pairs[int(len(pairs) * 0.9) - 1] if len(pairs) >= 10 else max(pairs)
    return Result(
        headline=(f'Median {median} day(s) from RFQ to quote, '
                  f'{p90} at the 90th percentile, over {len(pairs)} quotes.'),
        figures={'median_days': median, 'p90_days': p90,
                 'sample': len(pairs)},
        notes=([] if len(pairs) >= 10 else
               [f'Only {len(pairs)} quotes had both dates — treat the '
                f'percentile as indicative.']),
        sources=['rfqs.received_date', 'quotes.quote_date'])


# ══════════════════════════════════════════════════════════════════════
#  PIPELINE
# ══════════════════════════════════════════════════════════════════════
@intent('pipeline_value', 'Pipeline value',
        permission='module.funnels',
        params={'weighted': 'true to weight by probability'},
        personas=('sales', 'head', 'mgmt'), phase=1,
        examples=("what's my pipeline worth", 'total pipeline value',
                  'weighted pipeline'))
def pipeline_value(scope, params):
    from app import Opportunity

    weighted = str(params.get('weighted') or '').lower() in ('1', 'true',
                                                             'yes')
    opps = (sc_mod.opportunities(sc=scope)
            .filter(~Opportunity.stage.in_(_TERMINAL)).all())
    if not opps:
        return Result(headline='No open opportunities in your scope.',
                      empty=True, sources=['opportunities.stage'])

    total = sum(float(o.value_inr or 0) for o in opps)
    wtotal = sum(float(o.value_inr or 0) * (o.probability or 0) / 100.0
                 for o in opps)

    by_stage = {}
    for o in opps:
        s = by_stage.setdefault(o.stage or '—', {'n': 0, 'v': 0.0})
        s['n'] += 1
        s['v'] += float(o.value_inr or 0)

    rows = [{'Stage': k, 'Count': v['n'], 'Value': _money(v['v'])}
            for k, v in sorted(by_stage.items(), key=lambda kv: -kv[1]['v'])]
    head = (f'Weighted pipeline {_money(wtotal)} across {len(opps)} '
            f'opportunities (unweighted {_money(total)}).' if weighted
            else f'Open pipeline {_money(total)} across {len(opps)} '
                 f'opportunities.')
    return Result(headline=head, columns=['Stage', 'Count', 'Value'],
                  rows=rows,
                  figures={'total': total, 'weighted': wtotal,
                           'count': len(opps)},
                  sources=['opportunities.value_inr',
                           'opportunities.probability'])


@intent('top_opportunities', 'Biggest open opportunities',
        permission='module.funnels',
        params={'limit': 'how many to show (default 20)'},
        personas=('head', 'mgmt'), phase=1,
        examples=('our 20 biggest opportunities', 'largest open deals',
                  'top opportunities by value'))
def top_opportunities(scope, params):
    from app import Opportunity

    limit = min(int(params.get('limit') or 20), ROW_CAP)
    found = (sc_mod.opportunities(sc=scope)
             .filter(~Opportunity.stage.in_(_TERMINAL))
             .order_by(Opportunity.value_inr.desc().nullslast())
             .limit(limit).all())
    if not found:
        return Result(headline='No open opportunities in your scope.',
                      empty=True, sources=['opportunities.value_inr'])
    rows = [{'Opportunity': o.opp_number, 'Title': o.title or '—',
             'Value': _money(o.value_inr), 'Stage': o.stage,
             'Owner': o.owner_emp_code or '—',
             '_chip': {'type': 'opportunity', 'id': o.id,
                       'label': o.opp_number}} for o in found]
    return Result(headline=f'The {len(rows)} largest open opportunities.',
                  columns=['Opportunity', 'Title', 'Value', 'Stage', 'Owner'],
                  rows=rows, figures={'count': len(rows)},
                  sources=['opportunities.value_inr'])


@intent('stalled_deals', 'Stalled deals',
        permission='module.funnels',
        params={'days': 'days in the same stage (default 14)'},
        personas=('head', 'mgmt'), phase=2,
        examples=('which deals are stuck', 'stalled opportunities',
                  'deals not moving'))
def stalled_deals(scope, params):
    from app import Opportunity

    days = int(params.get('days') or 14)
    cutoff = _days_ago(days)
    found = (sc_mod.opportunities(sc=scope)
             .filter(~Opportunity.stage.in_(_TERMINAL))
             .filter(or_(Opportunity.updated_at.is_(None),
                         Opportunity.updated_at < cutoff))
             .order_by(Opportunity.value_inr.desc().nullslast()).all())
    if not found:
        return Result(headline=f'Nothing has sat still for {days}+ days.',
                      empty=True, sources=['opportunities.updated_at'])
    rows = [{'Opportunity': o.opp_number, 'Value': _money(o.value_inr),
             'Stage': o.stage, 'Days still': _age(o.updated_at) or 0,
             'Owner': o.owner_emp_code or '—',
             '_chip': {'type': 'opportunity', 'id': o.id,
                       'label': o.opp_number}} for o in found]
    rows, notes = _cap(rows, len(found))
    return Result(
        headline=f'{len(found)} opportunity(s) unchanged for {days}+ days.',
        columns=['Opportunity', 'Value', 'Stage', 'Days still', 'Owner'],
        rows=rows, figures={'count': len(found)}, notes=notes,
        sources=['opportunities.updated_at'])


# ══════════════════════════════════════════════════════════════════════
#  ACCOUNTS  ·  §5.1 flagship
# ══════════════════════════════════════════════════════════════════════
def _resolve_company(name):
    """Find an account by name. Existence is not scoped — see below."""
    from app import Company

    clean = (name or '').strip()
    if not clean:
        return None
    exact = Company.query.filter(func.lower(Company.name) ==
                                 clean.lower()).first()
    if exact:
        return exact
    return Company.query.filter(Company.name.ilike(f'%{clean}%')).first()


@intent('account_owner', 'Who handles this account',
        params={'account': 'the customer name'},
        personas=('sales', 'head', 'ops'), phase=1,
        examples=('who handles JSW', 'who owns Tata Steel',
                  'who is the PIC for Godrej'))
def account_owner(scope, params):
    """Routing is safe at every scope.

    §6.6 is explicit: never a blunt access-denied. Knowing who to talk
    to is what stops two salespeople approaching the same customer, and
    withholding it causes the exact duplication the CRM exists to
    prevent. Only the owner's name is given — no values, no history.
    """
    from app import Employee

    name = (params.get('account') or '').strip()
    if not name:
        return Result(headline='Which account?', empty=True)

    company = _resolve_company(name)
    if company is None:
        return Result(headline=f'No account matching "{name}" in the CRM.',
                      empty=True, sources=['companies.name'])

    def who(code):
        if not code:
            return None
        e = Employee.query.filter_by(emp_code=code).first()
        return e.name if e else code

    primary = who(company.pic_emp_code)
    secondary = who(company.secondary_pic_emp_code)
    if not primary:
        return Result(
            headline=(f'{company.name} is in the CRM but has no owner '
                      f'configured.'),
            notes=['An account with no owner cannot auto-assign its leads.'],
            sources=['companies.pic_emp_code'])

    bits = f'{company.name} — primary {primary}'
    if secondary:
        bits += f', secondary {secondary}'
    if company.vertical:
        bits += f' · {company.vertical}'
    return Result(headline=bits + '.',
                  figures={'primary': company.pic_emp_code,
                           'secondary': company.secondary_pic_emp_code or ''},
                  sources=['Account Master'])


@intent('account_status', 'Is this account already handled?',
        params={'account': 'the customer name'},
        personas=('sales', 'head', 'ops', 'mgmt'), phase=1,
        examples=('is Tata Steel already handled',
                  'do we already work with Siemens',
                  'is this a new customer'))
def account_status(scope, params):
    """§5.1 — the flagship.

    Two answers by design. Anyone gets existence plus routing, because
    that is what stops a second approach to an existing customer.
    Detail — values, history, live opportunities — needs entitlement,
    and its absence returns the routing form rather than an error.
    """
    from app import Employee, Opportunity
    from app.models.quote import Quote

    name = (params.get('account') or '').strip()
    if not name:
        return Result(headline='Which account?', empty=True)

    company = _resolve_company(name)
    if company is None:
        return Result(
            headline=(f'No account matching "{name}". Treat it as a new '
                      f'customer — nothing in the CRM says otherwise.'),
            empty=True, sources=['companies.name'])

    def who(code):
        if not code:
            return '—'
        e = Employee.query.filter_by(emp_code=code).first()
        return e.name if e else code

    routing = (f'{company.name} is already a Procam account. '
               f'Primary owner: {who(company.pic_emp_code)}')
    if company.secondary_pic_emp_code:
        routing += f' · Ops PIC: {who(company.secondary_pic_emp_code)}'
    if company.vertical:
        routing += f' · Vertical: {company.vertical}'

    entitled = (scope.can('reports.accounts')
                or scope.reaches(company.pic_emp_code or '')
                or scope.reaches(company.secondary_pic_emp_code or ''))
    if not entitled:
        return Result(
            headline=routing + ' — please contact the assigned account '
                                'owner for details.',
            restricted=True, sources=['Account Master'])

    open_opps = (sc_mod.opportunities(sc=scope)
                 .filter(Opportunity.company_id == company.id,
                         ~Opportunity.stage.in_(_TERMINAL)).all())
    last_quote = (sc_mod.quotes(sc=scope)
                  .filter(Quote.account_id == company.id)
                  .order_by(Quote.quote_date.desc()).first())

    detail = []
    if open_opps:
        detail.append({'Fact': 'Open opportunities',
                       'Value': f'{len(open_opps)} · '
                                f'{_money(sum(float(o.value_inr or 0) for o in open_opps))}'})
    if last_quote:
        detail.append({'Fact': 'Last quote',
                       'Value': f'{last_quote.quote_number} · '
                                f'{_money(last_quote.total_amount)} · '
                                f'{str(last_quote.quote_date or "")[:10]}'})
    if company.last_activity_at:
        detail.append({'Fact': 'Last activity',
                       'Value': str(company.last_activity_at)[:10]})
    if not detail:
        detail.append({'Fact': 'Activity',
                       'Value': 'No opportunities or quotes recorded'})

    return Result(headline=routing + '.', columns=['Fact', 'Value'],
                  rows=detail,
                  figures={'open_opportunities': len(open_opps)},
                  sources=['Account Master', 'opportunities', 'quotes'])


@intent('accounts_inactive', 'Accounts gone quiet',
        permission='reports.accounts',
        params={'days': 'days of silence (default 90)'},
        personas=('head', 'mgmt'), phase=2,
        examples=('which customers have gone quiet',
                  'inactive accounts', 'accounts we have not contacted'))
def accounts_inactive(scope, params):
    from app import Company

    days = int(params.get('days') or 90)
    cutoff = _days_ago(days)
    found = (sc_mod.companies(sc=scope)
             .filter(Company.is_active.is_(True))
             .filter(or_(Company.last_activity_at.is_(None),
                         Company.last_activity_at < cutoff))
             .order_by(Company.last_activity_at.asc().nullsfirst()).all())
    if not found:
        return Result(headline=f'Every account has been touched in the last '
                               f'{days} days.', empty=True,
                      sources=['companies.last_activity_at'])
    rows = [{'Account': c.name, 'Vertical': c.vertical or '—',
             'Owner': c.pic_emp_code or '—',
             'Last activity': str(c.last_activity_at or '')[:10] or 'never',
             '_chip': {'type': 'company', 'id': c.id, 'label': c.name}}
            for c in found]
    rows, notes = _cap(rows, len(found))
    return Result(headline=f'{len(found)} account(s) quiet for {days}+ days.',
                  columns=['Account', 'Vertical', 'Owner', 'Last activity'],
                  rows=rows, figures={'count': len(found)}, notes=notes,
                  sources=['companies.last_activity_at'])


# ══════════════════════════════════════════════════════════════════════
#  HANDOVERS
# ══════════════════════════════════════════════════════════════════════
@intent('handovers_recent', 'Recent handovers',
        permission='module.handovers',
        params={'days': 'window in days (default 30)'},
        personas=('ops', 'mgmt'), phase=1,
        examples=('what was handed over this month',
                  'recent handovers to operations'))
def handovers_recent(scope, params):
    from app.models.tms_handover import WonHandover

    days = int(params.get('days') or 30)
    found = (sc_mod.handovers(sc=scope)
             .filter(WonHandover.created_at >= _days_ago(days))
             .order_by(WonHandover.created_at.desc()).all()
             if hasattr(WonHandover, 'created_at') else
             sc_mod.handovers(sc=scope).all())
    if not found:
        return Result(headline=f'No handovers in the last {days} days.',
                      empty=True, sources=['won_handovers'])
    rows = [{'Account': h.account_name or '—',
             'Value': _money(h.won_value), 'Status': h.status or '—',
             '_chip': {'type': 'handover', 'id': h.id,
                       'label': h.account_name or f'Handover {h.id}'}}
            for h in found]
    rows, notes = _cap(rows, len(found))
    return Result(headline=f'{len(found)} handover(s) in the last {days} days.',
                  columns=['Account', 'Value', 'Status'], rows=rows,
                  figures={'count': len(found)}, notes=notes,
                  sources=['won_handovers.status'])


# ══════════════════════════════════════════════════════════════════════
#  SEARCH
# ══════════════════════════════════════════════════════════════════════
@intent('universal_search', 'Search everything',
        params={'term': 'what to look for'},
        personas=('sales', 'head', 'mgmt', 'ops'), phase=1,
        examples=('Tata', 'find Siemens', 'search for JSW'))
def universal_search(scope, params):
    """Each entity filtered by its own entitlement.

    Someone without module.quotes gets the same search without quote
    rows — not an error, and not a hint that quote rows were withheld.
    """
    from app import Company, Lead

    term = (params.get('term') or '').strip()
    if len(term) < 2:
        return Result(headline='Give me at least two characters to search on.',
                      empty=True)

    like = f'%{term}%'
    rows = []

    for c in (sc_mod.companies(sc=scope)
              .filter(Company.name.ilike(like)).limit(10).all()):
        rows.append({'Type': 'Account', 'Name': c.name,
                     'Detail': c.vertical or '—',
                     '_chip': {'type': 'company', 'id': c.id,
                               'label': c.name}})

    for l in (sc_mod.leads(sc=scope)
              .filter(or_(Lead.company.ilike(like),
                          Lead.project.ilike(like),
                          Lead.pic.ilike(like))).limit(15).all()):
        rows.append({'Type': 'Lead', 'Name': l.company,
                     'Detail': f'{l.stage} · {_money(l.estimated_value_inr)}',
                     '_chip': _lead_chip(l)})

    if scope.can('module.quotes'):
        from app.models.quote import Quote
        for q in (sc_mod.quotes(sc=scope)
                  .filter(or_(Quote.quote_number.ilike(like),
                              Quote.subject.ilike(like))).limit(10).all()):
            rows.append({'Type': 'Quote', 'Name': q.quote_number,
                         'Detail': _money(q.total_amount),
                         '_chip': {'type': 'quote', 'id': q.id,
                                   'label': q.quote_number}})

    if scope.can('module.rfq'):
        from app.models.rfq import RFQ
        for r in (sc_mod.rfqs(sc=scope)
                  .filter(or_(RFQ.rfq_number.ilike(like),
                              RFQ.subject.ilike(like))).limit(10).all()):
            rows.append({'Type': 'RFQ', 'Name': r.rfq_number,
                         'Detail': r.subject or '—',
                         '_chip': {'type': 'rfq', 'id': r.id,
                                   'label': r.rfq_number}})

    if not rows:
        return Result(headline=f'Nothing matching "{term}" that you can see.',
                      empty=True, sources=['companies', 'leads'])
    total = len(rows)
    rows, notes = _cap(rows, total)
    return Result(headline=f'{total} match(es) for "{term}".',
                  columns=['Type', 'Name', 'Detail'], rows=rows,
                  figures={'count': total}, notes=notes,
                  sources=['companies', 'leads', 'quotes', 'rfqs'])


# ══════════════════════════════════════════════════════════════════════
#  DATA QUALITY  ·  delegates to the existing module
# ══════════════════════════════════════════════════════════════════════
@intent('dq_lost_no_reason', 'Lost leads with no reason',
        permission='admin.master', personas=('admin',), phase=2,
        examples=('lost leads with no reason',
                  'which losses have no reason recorded'))
def dq_lost_no_reason(scope, params):
    from app import Lead

    found = (sc_mod.leads(sc=scope).filter(Lead.stage == 'Lost')
             .filter(or_(Lead.lost_reason.is_(None), Lead.lost_reason == ''))
             .order_by(Lead.updated_at.desc()).all())
    if not found:
        return Result(headline='Every lost lead has a reason recorded.',
                      empty=True, sources=['leads.lost_reason'])
    rows = [{'Company': l.company, 'Owner': l.assigned_to or '—',
             'Lost': str(l.updated_at or '')[:10], '_chip': _lead_chip(l)}
            for l in found]
    rows, notes = _cap(rows, len(found))
    return Result(headline=f'{len(found)} lost lead(s) with no reason.',
                  columns=['Company', 'Owner', 'Lost'], rows=rows,
                  figures={'count': len(found)}, notes=notes,
                  sources=['leads.stage', 'leads.lost_reason'])


# ══════════════════════════════════════════════════════════════════════
#  LOSS ANALYSIS  ·  honest about missing data
# ══════════════════════════════════════════════════════════════════════
#: Below this many populated rows, a percentage is theatre.
MIN_SAMPLE_FOR_ANALYSIS = 20


@intent('loss_analysis', 'Where we are losing',
        permission='reports.competitor', personas=('head', 'mgmt'), phase=2,
        examples=('where are we losing', 'why do we lose',
                  'top loss reasons'))
def loss_analysis(scope, params):
    """§5 lost-business analysis, gated on having the data.

    The Lost-Reason capture is being added separately. Until it has
    been populated for a while, this must say so rather than compute a
    percentage from a handful of rows — a confident figure drawn from
    thirty populated records out of two thousand is worse than no
    answer, because people act on it.
    """
    from app import Lead

    lost = sc_mod.leads(sc=scope).filter(Lead.stage == 'Lost').all()
    with_reason = [l for l in lost if (l.lost_reason or '').strip()]

    if not lost:
        return Result(headline='No lost leads in your scope.', empty=True,
                      sources=['leads.stage'])
    if len(with_reason) < MIN_SAMPLE_FOR_ANALYSIS:
        return Result(
            headline=(f'Not enough recorded loss reasons to analyse — '
                      f'{len(with_reason)} of {len(lost)} lost leads have '
                      f'one.'),
            empty=True,
            notes=['A breakdown from this few would look authoritative and '
                   'mean nothing. Ask again once reasons are being captured '
                   'consistently.'],
            sources=['leads.lost_reason'])

    counts = {}
    for l in with_reason:
        key = (l.lost_reason or '').strip()
        counts[key] = counts.get(key, 0) + 1
    rows = [{'Reason': k, 'Count': v,
             'Share': f'{round(100 * v / len(with_reason))}%'}
            for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
    coverage = round(100 * len(with_reason) / len(lost))
    return Result(
        headline=(f'{len(with_reason)} losses with a recorded reason; '
                  f'"{rows[0]["Reason"]}" is the most common.'),
        columns=['Reason', 'Count', 'Share'], rows=rows[:ROW_CAP],
        figures={'analysed': len(with_reason), 'total_lost': len(lost),
                 'coverage_pct': coverage},
        notes=([] if coverage >= 80 else
               [f'Only {coverage}% of lost leads have a reason recorded, so '
                f'this describes the ones that do.']),
        sources=['leads.stage', 'leads.lost_reason'])


# ══════════════════════════════════════════════════════════════════════
#  NEXT BEST ACTION  ·  every suggestion states its reason
# ══════════════════════════════════════════════════════════════════════
@intent('next_best_action', 'Who should I call',
        params={'limit': 'how many suggestions (default 10)'},
        personas=('sales', 'head'), phase=3,
        examples=('who should I call this week', 'what should I do next',
                  'next best action'))
def next_best_action(scope, params):
    """Ranked by value against staleness, with the reason drawn from the
    data. A recommendation whose reason the model invented would be a
    hallucination wearing a suggestion's clothes."""
    from app import Lead

    limit = min(int(params.get('limit') or 10), ROW_CAP)
    candidates = (sc_mod.leads(sc=scope)
                  .filter(~Lead.stage.in_(_TERMINAL)).all())
    if not candidates:
        return Result(headline='Nothing open to act on.', empty=True,
                      sources=['leads.stage'])

    scored = []
    for l in candidates:
        idle = _age(l.updated_at or l.created_at) or 0
        value = float(l.estimated_value_inr or 0)
        if idle < 2:
            continue
        reason = None
        if l.stage == 'Quoted' and idle >= 3:
            reason = (f'Quoted {idle} days ago with no movement since — '
                      f'follow up.')
        elif l.stage == 'Under Negotiation' and idle >= 5:
            reason = f'In negotiation and untouched for {idle} days.'
        elif l.followup_date and l.followup_date < _now().date():
            reason = (f'Follow-up was due '
                      f'{(_now().date() - l.followup_date).days} days ago.')
        elif idle >= 14:
            reason = f'No activity for {idle} days.'
        if not reason:
            continue
        scored.append((value * max(idle, 1), l, reason, idle, value))

    if not scored:
        return Result(headline='Nothing is overdue attention right now.',
                      empty=True, sources=['leads.updated_at'])

    scored.sort(key=lambda t: -t[0])
    rows = [{'Company': l.company, 'Stage': l.stage, 'Value': _money(v),
             'Idle days': idle, 'Why': reason, '_chip': _lead_chip(l)}
            for _s, l, reason, idle, v in scored[:limit]]
    return Result(headline=f'{len(rows)} lead(s) worth your attention first.',
                  columns=['Company', 'Stage', 'Value', 'Idle days', 'Why'],
                  rows=rows, figures={'count': len(scored)},
                  sources=['leads.updated_at', 'leads.followup_date',
                           'leads.stage'])
