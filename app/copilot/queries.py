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

import re
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



# ── what "last contact" actually means ───────────────────────────────
#
# One definition, in app/services/contact.py, shared with the Sales
# Intelligence tiles. Keeping a second copy here is how the tile and the
# question end up disagreeing on the same screen — which they did, until
# this was pulled out.

def _contacted_since(cutoff):
    from app.services import contact as _contact
    touched = _contact.contacted_since(cutoff)
    return touched, set()          # (acted, mailed) — callers union them


def _last_contact(lead):
    from app.services import contact as _contact
    return _contact.last_contact(lead)


def _nothing_recorded(label, module_hint=''):
    """The answer for a table that is empty, not a filter that matched.

    "Every RFQ in your scope has been quoted" is true when there are no
    RFQs, and reads as reassurance. In production both the rfqs and
    quotes tables held zero rows, so four intents were answering good
    news about modules nobody has started using. Vacuous truth is the
    quietest way for a system like this to mislead.
    """
    return Result(
        headline=f'There are no {label} recorded in the CRM at all.',
        empty=True,
        notes=([module_hint] if module_hint else []))


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
    # Same definition of contact as leads_stale, so the tile and the
    # question can never disagree about who has gone quiet.
    acted, mailed = _contacted_since(_days_ago(3))
    touched = acted | mailed
    idle = sum(1 for l in mine.with_entities(Lead.id).all()
               if l[0] not in touched)
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
        params={'days': 'how many days without contact (default 7)'},
        personas=('sales', 'head'), phase=1,
        examples=('leads with no activity for 7 days',
                  'which of my leads have gone quiet',
                  'stale leads', 'untouched leads'))
def leads_stale(scope, params):
    """Leads nobody has been in contact with.

    Contact means an activity OR an email, because in this CRM the
    email trail is where contact actually lives — seven activity rows
    against eleven thousand leads. Ranking on the log alone reported
    every open lead as neglected, which told a salesperson to chase
    people they had emailed that morning.
    """
    from app import Lead

    days = int(params.get('days') or DEFAULT_IDLE_DAYS)
    cutoff = _days_ago(days)
    acted, mailed = _contacted_since(cutoff)
    touched = acted | mailed

    open_q = sc_mod.leads(sc=scope).filter(~Lead.stage.in_(_TERMINAL))
    # Columns, not objects: every open lead is still read, because
    # "touched" is a Python set from the one contact definition, but as
    # six-column tuples rather than full rows.
    open_leads = (open_q.with_entities(*_lead_row_columns())
                  .order_by(Lead.created_at.asc(), Lead.id.asc()).all())
    found = [l for l in open_leads if l.id not in touched]

    if not found:
        return Result(
            headline=f'Every open lead has been contacted in the last '
                     f'{days} days.',
            empty=True,
            sources=['lead_activities.occurred_at',
                     'lead_emails.sent_or_received_at'])

    rows = []
    from app.services import contact as _contact
    last_seen = _contact.last_contacts([l.id for l in found[:ROW_CAP]])
    for l in found[:ROW_CAP]:
        last = last_seen.get(l.id)
        rows.append({'Company': l.company, 'Stage': l.stage,
                     'Value': _money(l.estimated_value_inr),
                     'Last contact': (str(last)[:10] if last else 'never'),
                     'Idle days': (_age(last) if last
                                   else _age(l.created_at) or 0),
                     'Owner': l.assigned_to or '—',
                     '_chip': _lead_chip(l)})
    notes = ([f'Showing the first {ROW_CAP} of {len(found)}.']
             if len(found) > ROW_CAP else [])

    # §6.8 — when nothing is recorded anywhere, say which it is
    # measuring. The test is whether contact EXISTS, not how many leads
    # came back stale: keying off the stale count fires this at a desk
    # that logged everything diligently a month ago, which is the
    # opposite of the case it is for. That mistake has now been made
    # twice, hence the explicit set.
    ever_acted, ever_mailed = _contacted_since(_days_ago(36500))
    ever = ever_acted | ever_mailed
    never = sum(1 for l in found if l.id not in ever)
    if open_leads and never >= len(open_leads):
        notes.append(
            f'All {len(open_leads)} of your open leads are here, and '
            f'neither an activity nor an email is recorded against any of '
            f'them. This is measuring what the CRM holds, not your '
            f'follow-up.')
    elif open_leads and never > len(open_leads) * 0.75:
        notes.append(
            f'{never} of your {len(open_leads)} open leads have no contact '
            f'recorded at all, so treat this as a lower bound.')

    return Result(
        headline=f'{len(found)} lead(s) with no contact for {days}+ days.',
        columns=['Company', 'Stage', 'Value', 'Last contact', 'Idle days',
                 'Owner'],
        rows=rows,
        figures={'count': len(found), 'days': days,
                 'open_total': len(open_leads), 'never_contacted': never},
        notes=notes,
        sources=['lead_activities.occurred_at',
                 'lead_emails.sent_or_received_at'])


def _lead_row_columns():
    """The Lead columns a list answer renders — and nothing else.

    Loading every open lead as a full ORM object to show fifty of them
    and count the rest was most of a quarter-second answer on a
    10,000-lead database: the notes, the email JSON and the history text
    all came back just to be thrown away. Rows are plain tuples with the
    same attribute names, so the row builders and _lead_chip read them
    unchanged.
    """
    from app import Lead
    return (Lead.id, Lead.company, Lead.stage, Lead.estimated_value_inr,
            Lead.assigned_to, Lead.created_at)


@intent('leads_open', 'Open leads',
        personas=('sales', 'head'), phase=1,
        examples=('my open leads', 'what leads are open',
                  'show me active leads'))
def leads_open(scope, params):
    from app import Lead

    base = sc_mod.leads(sc=scope).filter(~Lead.stage.in_(_TERMINAL))
    # The headline total is counted in SQL, so it stays exact while only
    # the rows the panel can show are loaded. Lead.id breaks ties in
    # created_at the way the stable sort over a rowid scan used to.
    total = base.count()
    if not total:
        return Result(headline='No open leads.', empty=True,
                      sources=['leads.stage'])
    found = (base.with_entities(*_lead_row_columns())
             .order_by(Lead.created_at.desc(), Lead.id.asc())
             .limit(ROW_CAP).all())
    rows = [{'Company': l.company, 'Stage': l.stage,
             'Value': _money(l.estimated_value_inr),
             'Owner': l.assigned_to or '—',
             'Age': _age(l.created_at) or 0, '_chip': _lead_chip(l)}
            for l in found]
    rows, notes = _cap(rows, total)
    return Result(headline=f'{total} open lead(s).',
                  columns=['Company', 'Stage', 'Value', 'Owner', 'Age'],
                  rows=rows, figures={'count': total}, notes=notes,
                  sources=['leads.stage'])


@intent('leads_no_next_action', 'Leads with no next action',
        personas=('sales', 'head', 'admin'), phase=1,
        examples=('leads with no follow-up date',
                  'which leads have no next action'))
def leads_no_next_action(scope, params):
    from app import Lead

    base = (sc_mod.leads(sc=scope)
            .filter(~Lead.stage.in_(_TERMINAL))
            .filter(Lead.followup_date.is_(None)))
    total = base.count()
    if not total:
        return Result(headline='Every open lead has a next action set.',
                      empty=True, sources=['leads.followup_date'])
    found = (base.with_entities(*_lead_row_columns())
             .order_by(Lead.created_at.desc(), Lead.id.asc())
             .limit(ROW_CAP).all())
    rows = [{'Company': l.company, 'Stage': l.stage,
             'Owner': l.assigned_to or '—',
             'Value': _money(l.estimated_value_inr), '_chip': _lead_chip(l)}
            for l in found]
    rows, notes = _cap(rows, total)
    return Result(
        headline=f'{total} open lead(s) with no next action recorded.',
        columns=['Company', 'Stage', 'Owner', 'Value'], rows=rows,
        figures={'count': total}, notes=notes,
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

    if not sc_mod.rfqs(sc=scope).count():
        return _nothing_recorded(
            'RFQs',
            'The RFQ module has no records yet, so this cannot tell you '
            'what is outstanding. Leads at the "RFQ Generated" stage are '
            'a separate count.')

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

    if not sc_mod.quotes(sc=scope).count():
        return _nothing_recorded(
            'quotes',
            'The Quotes module has no records yet.')

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

    if not sc_mod.quotes(sc=scope).count():
        return _nothing_recorded('quotes',
                                 'The Quotes module has no records yet.')

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

    has_owner = bool(company.pic_emp_code)
    routing = f'{company.name} is already a Procam account.'
    if has_owner:
        routing += f' Primary owner: {who(company.pic_emp_code)}'
        if company.secondary_pic_emp_code:
            routing += f' · Ops PIC: {who(company.secondary_pic_emp_code)}'
    else:
        routing += ' No owner is configured for it.'
    if company.vertical:
        routing += f' · Vertical: {company.vertical}'

    entitled = (scope.can('reports.accounts')
                or scope.reaches(company.pic_emp_code or '')
                or scope.reaches(company.secondary_pic_emp_code or ''))
    if not entitled:
        # "Contact the assigned account owner" is useless advice when
        # nobody is assigned — and it was what this said.
        follow = (' Contact the account owner for details.' if has_owner
                  else ' Nobody owns it yet, so ask an administrator to '
                       'assign it before you approach them.')
        return Result(headline=routing + follow, restricted=True,
                      notes=([] if has_owner else
                             ['An account with no owner cannot auto-assign '
                              'its leads.']),
                      sources=['Account Master'])

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
    base = (sc_mod.companies(sc=scope)
            .filter(Company.is_active.is_(True))
            .filter(or_(Company.last_activity_at.is_(None),
                        Company.last_activity_at < cutoff)))
    total = base.count()
    if not total:
        return Result(headline=f'Every account has been touched in the last '
                               f'{days} days.', empty=True,
                      sources=['companies.last_activity_at'])
    found = (base.with_entities(Company.id, Company.name, Company.vertical,
                                Company.pic_emp_code,
                                Company.last_activity_at)
             .order_by(Company.last_activity_at.asc().nullsfirst(),
                       Company.id.asc())
             .limit(ROW_CAP).all())
    rows = [{'Account': c.name, 'Vertical': c.vertical or '—',
             'Owner': c.pic_emp_code or '—',
             'Last activity': str(c.last_activity_at or '')[:10] or 'never',
             '_chip': {'type': 'company', 'id': c.id, 'label': c.name}}
            for c in found]
    rows, notes = _cap(rows, total)
    return Result(headline=f'{total} account(s) quiet for {days}+ days.',
                  columns=['Account', 'Vertical', 'Owner', 'Last activity'],
                  rows=rows, figures={'count': total}, notes=notes,
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

    if not sc_mod.handovers(sc=scope).count():
        return _nothing_recorded('handovers')

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

    base = (sc_mod.leads(sc=scope).filter(Lead.stage == 'Lost')
            .filter(or_(Lead.lost_reason.is_(None), Lead.lost_reason == '')))
    total = base.count()
    if not total:
        return Result(headline='Every lost lead has a reason recorded.',
                      empty=True, sources=['leads.lost_reason'])
    found = (base.with_entities(Lead.id, Lead.company, Lead.assigned_to,
                                Lead.updated_at)
             .order_by(Lead.updated_at.desc(), Lead.id.asc())
             .limit(ROW_CAP).all())
    rows = [{'Company': l.company, 'Owner': l.assigned_to or '—',
             'Lost': str(l.updated_at or '')[:10], '_chip': _lead_chip(l)}
            for l in found]
    rows, notes = _cap(rows, total)
    return Result(headline=f'{total} lost lead(s) with no reason.',
                  columns=['Company', 'Owner', 'Lost'], rows=rows,
                  figures={'count': total}, notes=notes,
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

    # Only the reason column: the analysis never reads anything else.
    lost = (sc_mod.leads(sc=scope).filter(Lead.stage == 'Lost')
            .with_entities(Lead.lost_reason).all())
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
                  .filter(~Lead.stage.in_(_TERMINAL))
                  .with_entities(Lead.id, Lead.company, Lead.stage,
                                 Lead.estimated_value_inr,
                                 Lead.followup_date, Lead.created_at)
                  .all())
    if not candidates:
        return Result(headline='Nothing open to act on.', empty=True,
                      sources=['leads.stage'])

    # Idle means no contact, not no edit — see _last_contact. Read for
    # every contacted lead in two grouped queries; one pair of queries
    # per lead was most of this answer's time on a large desk.
    from app.services import contact as _contact
    acted, mailed = _contacted_since(_days_ago(3650))
    ever = acted | mailed
    last_seen = _contact.last_contacts() if ever else {}

    scored = []
    for l in candidates:
        if l.id in ever:
            last = last_seen.get(l.id)
            idle = _age(last) or 0
        else:
            idle = _age(l.created_at) or 0
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
            reason = (f'No contact for {idle} days.' if l.id in ever
                      else 'No contact has ever been recorded.')
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
                  recommendation=True,
                  columns=['Company', 'Stage', 'Value', 'Idle days', 'Why'],
                  rows=rows, figures={'count': len(scored)},
                  sources=['lead_emails.sent_or_received_at',
                           'lead_activities.occurred_at',
                           'leads.followup_date', 'leads.stage'])


# ══════════════════════════════════════════════════════════════════════
#  PHASE 4  ·  DOCUMENT AND EMAIL INTELLIGENCE  ·  §8
# ══════════════════════════════════════════════════════════════════════
#
# The one place record text reaches the model. It is wrapped and
# labelled untrusted, but the defence that matters is structural: the
# rows were selected by a scoped query before any text was read, so an
# instruction inside an email cannot widen what was retrieved.

def _resolve_lead(scope, params):
    """The lead a question is about — by id, by context, or by name."""
    from app import Lead

    lead_id = params.get('lead_id') or (params.get('context') or {}).get('id')
    if lead_id:
        lead = sc_mod.leads(sc=scope).filter(Lead.id == int(lead_id)).first()
        if lead:
            return lead
        return None
    name = (params.get('account') or params.get('lead') or '').strip()
    if not name:
        return None
    return (sc_mod.leads(sc=scope)
            .filter(Lead.company.ilike(f'%{name}%'))
            .order_by(Lead.updated_at.desc().nullslast()).first())


@intent('thread_summary', 'What the customer last asked',
        params={'lead_id': 'the lead in context',
                'account': 'or the customer name'},
        personas=('sales', 'head'), phase=4,
        examples=('what did the customer ask in the latest email',
                  'summarise the email thread', 'what was the last email'))
def thread_summary(scope, params):
    """§8 — so nobody reads twenty mails to find the question."""
    from app import LeadEmail

    lead = _resolve_lead(scope, params)
    if lead is None:
        return Result(headline='Which lead? Open one, or name the customer.',
                      empty=True)

    # The lead was already resolved through a scoped query, so its
    # emails are in scope by construction and this filter changes
    # nothing today. Kept as defence in depth: it is what stops a future
    # caller that resolves the lead some other way from leaking a trail.
    trail = (sc_mod.emails(sc=scope)
             .filter(LeadEmail.lead_id == lead.id)
             .order_by(LeadEmail.sent_or_received_at.desc().nullslast())
             .limit(20).all())
    if not trail:
        original = (lead.original_email_body or '').strip()
        if not original:
            return Result(
                headline=f'No email recorded against {lead.company}.',
                empty=True, sources=['lead_emails'])
        return Result(
            headline=f'{lead.company} — the original enquiry, no reply '
                     f'trail recorded.',
            rows=[{'Direction': 'inbound',
                   'When': str(lead.original_email_received_at or '')[:10],
                   'Text': original[:600]}],
            columns=['Direction', 'When', 'Text'],
            sources=['leads.original_email_body'])

    inbound = [e for e in trail if (e.direction or '') == 'inbound']
    latest = inbound[0] if inbound else trail[0]
    rows = [{'Direction': e.direction or '—',
             'When': str(e.sent_or_received_at or '')[:16],
             'From': e.from_addr or '—',
             'Subject': (e.subject or '')[:80],
             'Text': (e.body or '')[:400]}
            for e in trail[:8]]
    return Result(
        headline=(f'{lead.company} — {len(trail)} message(s), the latest '
                  f'inbound on {str(latest.sent_or_received_at or "")[:10]}.'),
        columns=['Direction', 'When', 'From', 'Subject', 'Text'], rows=rows,
        figures={'messages': len(trail), 'inbound': len(inbound)},
        notes=(['Showing the most recent 8 of '
                f'{len(trail)}.'] if len(trail) > 8 else []),
        sources=[f'Email trail on lead #{lead.id}'])


@intent('attachment_contents', 'What is in the attachments',
        params={'lead_id': 'the lead in context',
                'account': 'or the customer name'},
        personas=('sales', 'head', 'ops'), phase=4,
        examples=('what is in the attachment', 'read the RFQ attachment',
                  'what does the BOQ say', 'what weight is in the cargo list'))
def attachment_contents(scope, params):
    """§8 — the requirement is usually in the spreadsheet, not the body."""
    from app import LeadAttachment
    from app.services import attachment_text

    lead = _resolve_lead(scope, params)
    if lead is None:
        return Result(headline='Which lead? Open one, or name the customer.',
                      empty=True)

    rows = (LeadAttachment.query.filter_by(lead_id=lead.id).limit(12).all())
    if not rows:
        return Result(headline=f'No attachments on {lead.company}.',
                      empty=True, sources=['lead_attachments'])

    readable, unread = [], []
    for att in rows:
        name = getattr(att, 'filename', None) or getattr(
            att, 'original_name', '') or f'attachment {att.id}'
        text = attachment_text.extract(getattr(att, 'storage_path', '') or '')
        if text:
            readable.append({'File': name, 'Extract': text[:700]})
        else:
            unread.append(name)

    notes = []
    if unread:
        notes.append('Could not read: ' + ', '.join(unread[:6]) + '.')
    if not attachment_text.pdf_supported():
        notes.append('PDF reading is unavailable on this host — install '
                     'pypdf to include them.')
    if not readable:
        return Result(
            headline=(f'{len(rows)} attachment(s) on {lead.company}, none '
                      f'readable as text.'),
            empty=True, notes=notes, sources=['lead_attachments'])

    return Result(
        headline=f'Read {len(readable)} of {len(rows)} attachment(s) on '
                 f'{lead.company}.',
        columns=['File', 'Extract'], rows=readable,
        figures={'readable': len(readable), 'total': len(rows)},
        notes=notes,
        sources=[f'Attachments on lead #{lead.id}'])


@intent('lead_360', 'Summarise this lead',
        params={'lead_id': 'the lead in context',
                'account': 'or the customer name'},
        personas=('sales', 'head'), phase=3,
        examples=('summarise this lead', 'brief me on this lead',
                  'lead 360', "what's the story with this lead"))
def lead_360(scope, params):
    """§5's meeting-prep answer: everything about one lead, assembled.

    The model narrates the assembly. Every fact in it is a column read
    here, so there is nothing for it to invent.
    """
    from app import LeadActivity, LeadEmail
    from app.models.quote import Quote

    lead = _resolve_lead(scope, params)
    if lead is None:
        return Result(headline='Which lead? Open one, or name the customer.',
                      empty=True)

    facts = [
        {'Fact': 'Stage', 'Value': lead.stage or '—'},
        {'Fact': 'Value', 'Value': _money(lead.estimated_value_inr)},
        {'Fact': 'Owner', 'Value': lead.assigned_to or 'unassigned'},
    ]
    if lead.secondary_owner:
        facts.append({'Fact': 'Secondary PIC', 'Value': lead.secondary_owner})
    if lead.procam_vertical:
        facts.append({'Fact': 'Vertical', 'Value': lead.procam_vertical})

    last_act = (sc_mod.activities(sc=scope)
                .filter(LeadActivity.lead_id == lead.id)
                .order_by(LeadActivity.occurred_at.desc().nullslast())
                .first())
    facts.append({'Fact': 'Last activity',
                  'Value': (f'{last_act.kind} · '
                            f'{str(last_act.occurred_at or "")[:10]}')
                  if last_act else 'none recorded'})

    quotes_n = (sc_mod.quotes(sc=scope)
                .filter(Quote.lead_id == lead.id).count()
                if scope.can('module.quotes') else None)
    if quotes_n is not None:
        facts.append({'Fact': 'Quotes', 'Value': str(quotes_n)})

    mails = (sc_mod.emails(sc=scope)
             .filter(LeadEmail.lead_id == lead.id).count())
    facts.append({'Fact': 'Emails on file', 'Value': str(mails)})

    facts.append({'Fact': 'Next action',
                  'Value': (str(lead.followup_date)
                            if lead.followup_date else
                            'none recorded — worth setting one')})

    notes = []
    if not lead.followup_date and (lead.stage or '') not in _TERMINAL:
        notes.append('Open lead with no follow-up date recorded.')
    if not last_act:
        notes.append('No activity has ever been logged against this lead.')

    return Result(
        headline=(f'{lead.company} — {lead.stage}, '
                  f'{_money(lead.estimated_value_inr)}, owned by '
                  f'{lead.assigned_to or "nobody"}.'),
        columns=['Fact', 'Value'], rows=facts, notes=notes,
        figures={'lead_id': lead.id},
        sources=[f'Lead #{lead.id}', 'lead_activities', 'quotes',
                 'lead_emails'])


@intent('account_360', 'Summarise this account',
        permission='reports.accounts',
        params={'account': 'the customer name'},
        personas=('head', 'mgmt', 'ops'), phase=3,
        examples=('summarise Tata Steel', 'brief me on JSW',
                  'account 360 for Godrej'))
def account_360(scope, params):
    from app import Contact, Employee, Lead, Opportunity
    from app.models.quote import Quote

    name = (params.get('account') or '').strip()
    company = _resolve_company(name) if name else None
    if company is None:
        return Result(headline=f'No account matching "{name}".', empty=True,
                      sources=['companies.name'])

    def who(code):
        if not code:
            return '—'
        e = Employee.query.filter_by(emp_code=code).first()
        return e.name if e else code

    opps = (sc_mod.opportunities(sc=scope)
            .filter(Opportunity.company_id == company.id).all())
    open_opps = [o for o in opps if (o.stage or '') not in _TERMINAL]
    won = [o for o in opps if (o.stage or '') == 'Won']
    leads_n = (sc_mod.leads(sc=scope)
               .filter(Lead.company_id == company.id).count())
    contacts_n = Contact.query.filter_by(company_id=company.id).count()

    facts = [
        {'Fact': 'Owner', 'Value': who(company.pic_emp_code)},
        {'Fact': 'Secondary PIC',
         'Value': who(company.secondary_pic_emp_code)},
        {'Fact': 'Vertical', 'Value': company.vertical or '—'},
        {'Fact': 'Contacts', 'Value': str(contacts_n)},
        {'Fact': 'Leads', 'Value': str(leads_n)},
        {'Fact': 'Open opportunities',
         'Value': f'{len(open_opps)} · '
                  f'{_money(sum(float(o.value_inr or 0) for o in open_opps))}'},
        {'Fact': 'Won',
         'Value': f'{len(won)} · '
                  f'{_money(sum(float(o.value_inr or 0) for o in won))}'},
        {'Fact': 'Last activity',
         'Value': str(company.last_activity_at or '')[:10] or 'never'},
    ]
    if scope.can('module.quotes'):
        last_q = (sc_mod.quotes(sc=scope)
                  .filter(Quote.account_id == company.id)
                  .order_by(Quote.quote_date.desc()).first())
        facts.append({'Fact': 'Last quote',
                      'Value': (f'{last_q.quote_number} · '
                                f'{_money(last_q.total_amount)} · '
                                f'{str(last_q.quote_date or "")[:10]}')
                      if last_q else 'none'})

    notes = []
    if not company.pic_emp_code:
        notes.append('No owner configured — leads from this account cannot '
                     'auto-assign.')
    return Result(
        headline=(f'{company.name} — {len(open_opps)} open opportunity(s), '
                  f'{len(won)} won, owned by {who(company.pic_emp_code)}.'),
        columns=['Fact', 'Value'], rows=facts, notes=notes,
        figures={'open': len(open_opps), 'won': len(won)},
        sources=['Account Master', 'opportunities', 'quotes'])


# ══════════════════════════════════════════════════════════════════════
#  THE MATRIX ROWS THAT HAD NO INTENT BEHIND THEM
# ══════════════════════════════════════════════════════════════════════
#
# Nine rows of the §14 matrix were specified and never built. They are
# here now, which is the difference between a design document and a
# deliverable.

@intent('pipeline_by_stage', 'Pipeline broken down',
        permission='module.funnels',
        params={'by': 'stage, vertical, owner or city (default stage)'},
        personas=('head', 'mgmt'), phase=1,
        examples=('pipeline by stage', 'pipeline by vertical',
                  'pipeline by owner', 'break down the pipeline'))
def pipeline_by_stage(scope, params):
    """§5 asks for pipeline by vertical, owner, city and stage. One
    intent with a dimension parameter, rather than four that drift."""
    from app import Company, Opportunity

    dimension = (params.get('by') or 'stage').strip().lower()
    opps = (sc_mod.opportunities(sc=scope)
            .filter(~Opportunity.stage.in_(_TERMINAL)).all())
    if not opps:
        return Result(headline='No open opportunities in your scope.',
                      empty=True, sources=['opportunities.stage'])

    companies = {}
    if dimension in ('vertical', 'city', 'customer', 'account'):
        for c in Company.query.with_entities(
                Company.id, Company.name, Company.vertical,
                Company.city).all():
            companies[c[0]] = c

    def key_for(o):
        if dimension == 'owner':
            return o.owner_emp_code or 'unassigned'
        if dimension in ('vertical', 'city', 'customer', 'account'):
            row = companies.get(o.company_id)
            if row is None:
                return 'unmapped account'
            return {'vertical': row[2], 'city': row[3],
                    'customer': row[1], 'account': row[1]}[dimension] or '—'
        return o.stage or '—'

    buckets = {}
    for o in opps:
        b = buckets.setdefault(key_for(o), {'n': 0, 'v': 0.0})
        b['n'] += 1
        b['v'] += float(o.value_inr or 0)

    label = {'owner': 'Owner', 'vertical': 'Vertical', 'city': 'City',
             'customer': 'Customer', 'account': 'Customer'}.get(
                 dimension, 'Stage')
    rows = [{label: k, 'Count': v['n'], 'Value': _money(v['v'])}
            for k, v in sorted(buckets.items(), key=lambda kv: -kv[1]['v'])]
    rows, notes = _cap(rows, len(rows))
    total = sum(float(o.value_inr or 0) for o in opps)
    return Result(
        headline=(f'{_money(total)} across {len(opps)} opportunities, '
                  f'by {label.lower()}.'),
        columns=[label, 'Count', 'Value'], rows=rows,
        figures={'total': total, 'count': len(opps), 'by': dimension},
        notes=notes, sources=['opportunities.value_inr'])


@intent('team_activity_gap', "Who hasn't updated the CRM",
        permission='reports.action',
        params={'days': 'window in days (default 7)'},
        personas=('head', 'mgmt'), phase=2,
        examples=("who hasn't updated CRM this week",
                  'who has not logged anything', 'team activity gap'))
def team_activity_gap(scope, params):
    from app import Employee, Lead, LeadActivity, LeadEmail

    days = int(params.get('days') or 7)
    cutoff = _days_ago(days)

    people = sc_mod.employees(sc=scope).filter(
        Employee.is_active.is_(True)).all()
    if not people:
        return Result(headline='Nobody in your scope.', empty=True,
                      sources=['employees'])

    active = {r[0] for r in LeadActivity.query
              .with_entities(LeadActivity.performed_by)
              .filter(LeadActivity.occurred_at >= cutoff).all() if r[0]}
    # An email sent from the trail counts too — see _last_contact.
    mailed_leads = {r[0] for r in LeadEmail.query
                    .with_entities(LeadEmail.lead_id)
                    .filter(LeadEmail.sent_or_received_at >= cutoff).all()}
    if mailed_leads:
        for r in Lead.query.with_entities(Lead.assigned_to).filter(
                Lead.id.in_(list(mailed_leads)[:5000])).all():
            if r[0]:
                active.add(r[0])

    quiet = [e for e in people if e.emp_code not in active]
    if not quiet:
        return Result(
            headline=f'Everyone has logged something in the last {days} '
                     f'days.', empty=True,
            sources=['lead_activities.performed_by'])

    rows = [{'Person': e.name or e.emp_code, 'Code': e.emp_code,
             'Vertical': e.vertical or '—',
             'Open leads': sc_mod.leads(sc=scope).filter(
                 Lead.assigned_to == e.emp_code,
                 ~Lead.stage.in_(_TERMINAL)).count()}
            for e in quiet]
    rows.sort(key=lambda r: -r['Open leads'])
    rows, notes = _cap(rows, len(rows))
    if len(active) == 0:
        notes.append('Nobody at all has logged anything, which usually '
                     'means the activity log is not in use rather than '
                     'that the team has stopped working.')
    return Result(
        headline=f'{len(quiet)} of {len(people)} have logged nothing in '
                 f'{days} days.',
        columns=['Person', 'Code', 'Vertical', 'Open leads'], rows=rows,
        figures={'quiet': len(quiet), 'team': len(people)}, notes=notes,
        sources=['lead_activities.performed_by',
                 'lead_emails.sent_or_received_at'])


@intent('conversion_rate', 'Conversion rate',
        permission='reports.accounts',
        params={'days': 'window in days (default 365)'},
        personas=('head', 'mgmt'), phase=2,
        examples=('what is my conversion rate', 'quote to order conversion',
                  'win rate'))
def conversion_rate(scope, params):
    from app import Opportunity

    days = int(params.get('days') or 365)
    since = _days_ago(days)
    opps = (sc_mod.opportunities(sc=scope)
            .filter(Opportunity.created_at >= since).all()
            if hasattr(Opportunity, 'created_at')
            else sc_mod.opportunities(sc=scope).all())

    decided = [o for o in opps if (o.stage or '') in ('Won', 'Lost')]
    won = [o for o in decided if o.stage == 'Won']
    if len(decided) < MIN_SAMPLE_FOR_ANALYSIS:
        return Result(
            headline=(f'Only {len(decided)} opportunity(s) have been won or '
                      f'lost in the last {days} days — too few to quote a '
                      f'rate.'),
            empty=True,
            notes=['A percentage from this few would look authoritative '
                   'and mean nothing.'],
            sources=['opportunities.stage'])

    pct = round(100 * len(won) / len(decided))
    won_value = sum(float(o.value_inr or 0) for o in won)
    return Result(
        headline=(f'{pct}% won — {len(won)} of {len(decided)} decided '
                  f'opportunities in the last {days} days, '
                  f'{_money(won_value)}.'),
        figures={'rate_pct': pct, 'won': len(won), 'decided': len(decided),
                 'won_value': won_value, 'window_days': days},
        notes=[f'Measured over the {days} days to today, on '
               f'opportunities that reached Won or Lost.'],
        sources=['opportunities.stage', 'opportunities.value_inr'])


@intent('daily_digest', 'What happened today',
        permission='reports.action',
        params={'days': 'window in days (default 1)'},
        personas=('mgmt', 'head'), phase=2,
        examples=('what happened in sales today', 'sales today',
                  "today's activity", 'what changed today'))
def daily_digest(scope, params):
    from app import Lead, LeadStageHistory, Opportunity

    days = int(params.get('days') or 1)
    since = _days_ago(days)

    new_leads = (sc_mod.leads(sc=scope)
                 .filter(Lead.created_at >= since).count())
    moves = (LeadStageHistory.query
             .filter(LeadStageHistory.changed_at >= since)
             .filter(LeadStageHistory.lead_id.in_(
                 sc_mod.leads(sc=scope).with_entities(Lead.id))).count())
    won = (sc_mod.opportunities(sc=scope)
           .filter(Opportunity.stage == 'Won',
                   Opportunity.won_at >= since).all())
    lost = (sc_mod.leads(sc=scope)
            .filter(Lead.stage == 'Lost', Lead.updated_at >= since).count())

    figures = {'new_leads': new_leads, 'stage_moves': moves,
               'won': len(won),
               'won_value': sum(float(o.value_inr or 0) for o in won),
               'lost': lost, 'window_days': days}
    if not any((new_leads, moves, len(won), lost)):
        return Result(
            headline=f'Nothing moved in the last {days} day(s).',
            figures=figures, empty=True,
            sources=['leads', 'lead_stage_history', 'opportunities'])
    return Result(
        headline=(f'{new_leads} new lead(s) · {moves} stage move(s) · '
                  f'{len(won)} won ({_money(figures["won_value"])}) · '
                  f'{lost} lost.'),
        figures=figures,
        sources=['leads.created_at', 'lead_stage_history.changed_at',
                 'opportunities.won_at'])


@intent('rfqs_high_value_recent', 'New high-value RFQs',
        permission='module.rfq',
        params={'amount': 'the threshold in rupees',
                'days': 'window in days (default 7)'},
        personas=('mgmt', 'head'), phase=2,
        examples=('new RFQs above 50 lakh this week',
                  'big RFQs this week', 'high value RFQs'))
def rfqs_high_value_recent(scope, params):
    from app import Opportunity
    from app.models.rfq import RFQ

    if not sc_mod.rfqs(sc=scope).count():
        return _nothing_recorded('RFQs',
                                 'The RFQ module has no records yet.')
    days = int(params.get('days') or 7)
    threshold = float(params.get('amount') or 5_000_000)
    recent = (sc_mod.rfqs(sc=scope)
              .filter(RFQ.received_date >= _days_ago(days).date()).all())
    rows = []
    for r in recent:
        value = 0.0
        if r.opportunity_id:
            opp = Opportunity.query.get(r.opportunity_id)
            value = float(opp.value_inr or 0) if opp else 0.0
        if value >= threshold:
            rows.append({'RFQ': r.rfq_number, 'Subject': r.subject,
                         'Value': _money(value),
                         'Received': str(r.received_date or '')[:10],
                         'Owner': r.lead_driver or '—',
                         '_chip': {'type': 'rfq', 'id': r.id,
                                   'label': r.rfq_number}})
    if not rows:
        return Result(
            headline=(f'No RFQs above {_money(threshold)} in the last '
                      f'{days} days.'), empty=True,
            sources=['rfqs.received_date', 'opportunities.value_inr'])
    rows.sort(key=lambda x: x['Received'], reverse=True)
    capped, notes = _cap(rows, len(rows))
    return Result(
        headline=(f'{len(rows)} RFQ(s) above {_money(threshold)} in the '
                  f'last {days} days.'),
        columns=['RFQ', 'Subject', 'Value', 'Received', 'Owner'],
        rows=capped, figures={'count': len(rows)}, notes=notes,
        sources=['rfqs.received_date', 'opportunities.value_inr'])


@intent('cross_sell_gap', 'Cross-sell gaps',
        permission='reports.accounts',
        params={'has': 'the vertical they already use',
                'missing': 'the vertical they do not'},
        personas=('head', 'mgmt'), phase=2,
        examples=('which accounts use Transport but never Warehousing',
                  'cross sell opportunities', 'single service accounts'))
def cross_sell_gap(scope, params):
    """§5 — "used Sea Freight but not Project Logistics".

    With no parameters it answers the more useful general question:
    which accounts have only ever bought one thing.
    """
    from app import Company, Opportunity

    has = (params.get('has') or '').strip()
    missing = (params.get('missing') or '').strip()

    won = (sc_mod.opportunities(sc=scope)
           .filter(Opportunity.stage == 'Won').all())
    if not won:
        return Result(headline='No won business recorded in your scope.',
                      empty=True, sources=['opportunities.stage'])

    verticals = {}
    for c in Company.query.with_entities(Company.id, Company.name,
                                         Company.vertical).all():
        verticals[c[0]] = (c[1], c[2])

    by_account = {}
    for o in won:
        name, vert = verticals.get(o.company_id, (None, None))
        if not name:
            continue
        by_account.setdefault(name, set()).add(vert or 'unspecified')

    if has and missing:
        rows = [{'Account': n, 'Uses': ', '.join(sorted(v))}
                for n, v in by_account.items()
                if has in v and missing not in v]
        head = (f'{len(rows)} account(s) use {has} but not {missing}.')
    else:
        rows = [{'Account': n, 'Uses': ', '.join(sorted(v))}
                for n, v in by_account.items() if len(v) == 1]
        head = f'{len(rows)} account(s) have only ever bought one service.'

    if not rows:
        return Result(headline='No cross-sell gap found.', empty=True,
                      sources=['opportunities.stage', 'companies.vertical'])
    rows.sort(key=lambda r: r['Account'])
    capped, notes = _cap(rows, len(rows))
    if all(r['Uses'] == 'unspecified' for r in capped):
        notes.append('No vertical is recorded on these accounts, so this '
                     'is grouping "unspecified" rather than a service.')
    return Result(headline=head, columns=['Account', 'Uses'], rows=capped,
                  figures={'count': len(rows)}, notes=notes,
                  sources=['opportunities.stage', 'companies.vertical'])


@intent('handover_missing_po', 'Won deals with no PO',
        permission='module.handovers', personas=('ops', 'mgmt'), phase=2,
        examples=('won deals with no PO', 'handovers missing a PO',
                  'which wins have no purchase order'))
def handover_missing_po(scope, params):
    from app import Opportunity
    from app.models.tms_handover import WonHandover

    won = (sc_mod.opportunities(sc=scope)
           .filter(Opportunity.stage == 'Won').all())
    if not won:
        return Result(headline='No won opportunities in your scope.',
                      empty=True, sources=['opportunities.stage'])

    have = {h.opportunity_id for h in
            WonHandover.query.with_entities(
                WonHandover.opportunity_id).all() if h[0]}
    missing = [o for o in won if o.id not in have]
    if not missing:
        return Result(headline='Every won deal has a handover recorded.',
                      empty=True, sources=['won_handovers'])
    rows = [{'Opportunity': o.opp_number, 'Value': _money(o.value_inr),
             'Won': str(o.won_at or '')[:10],
             'Days since': _age(o.won_at) or 0,
             'Owner': o.owner_emp_code or '—',
             '_chip': {'type': 'opportunity', 'id': o.id,
                       'label': o.opp_number}} for o in missing]
    rows.sort(key=lambda r: -r['Days since'])
    capped, notes = _cap(rows, len(rows))
    return Result(
        headline=(f'{len(missing)} won deal(s) with no handover or PO '
                  f'captured — one PO to one project and job.'),
        columns=['Opportunity', 'Value', 'Won', 'Days since', 'Owner'],
        rows=capped, figures={'count': len(missing)}, notes=notes,
        sources=['opportunities.stage', 'won_handovers.opportunity_id'])


@intent('dq_missing_fields', 'Records missing key fields',
        permission='admin.master', personas=('admin', 'head'), phase=2,
        examples=('which leads are missing an owner',
                  'data quality', 'incomplete records',
                  'leads with no value'))
def dq_missing_fields(scope, params):
    """§5's data-quality assistant. Counts by defect so an admin can see
    which gap is worth a campaign, rather than one list of everything."""
    from app import Lead, Opportunity

    open_leads = sc_mod.leads(sc=scope).filter(
        ~Lead.stage.in_(_TERMINAL))
    checks = [
        ('Lead has no owner',
         open_leads.filter(or_(Lead.assigned_to.is_(None),
                               Lead.assigned_to == '')).count()),
        ('Lead has no next action',
         open_leads.filter(Lead.followup_date.is_(None)).count()),
        ('Lead has no value',
         open_leads.filter(or_(Lead.estimated_value_inr.is_(None),
                               Lead.estimated_value_inr == 0)).count()),
        ('Lead has no vertical',
         open_leads.filter(or_(Lead.procam_vertical.is_(None),
                               Lead.procam_vertical == '')).count()),
        ('Lost lead has no reason',
         sc_mod.leads(sc=scope).filter(
             Lead.stage == 'Lost',
             or_(Lead.lost_reason.is_(None),
                 Lead.lost_reason == '')).count()),
        ('Opportunity has no close date',
         sc_mod.opportunities(sc=scope).filter(
             ~Opportunity.stage.in_(_TERMINAL),
             Opportunity.expected_close_date.is_(None)).count()),
        ('Opportunity has no owner',
         sc_mod.opportunities(sc=scope).filter(
             or_(Opportunity.owner_emp_code.is_(None),
                 Opportunity.owner_emp_code == '')).count()),
    ]
    rows = [{'Gap': name, 'Records': n} for name, n in checks if n]
    if not rows:
        return Result(headline='No missing fields found in your scope.',
                      empty=True, sources=['Data Quality'])
    rows.sort(key=lambda r: -r['Records'])
    return Result(
        headline=f'{sum(r["Records"] for r in rows)} record(s) with a '
                 f'missing field, across {len(rows)} kinds of gap.',
        columns=['Gap', 'Records'], rows=rows,
        figures={k: v for k, v in checks},
        notes=['The Data Quality screen lists the individual records and '
               'can fix them in bulk.'],
        sources=['Data Quality module'])


@intent('dq_duplicates', 'Possible duplicate accounts',
        permission='admin.master', personas=('admin',), phase=2,
        examples=('are there duplicate accounts', 'duplicate customers',
                  'accounts with the same name'))
def dq_duplicates(scope, params):
    """Presents; it does not re-score. The company-match service already
    owns that judgement and a second opinion here would drift from it."""
    import re as _re
    from collections import defaultdict

    from app import Company

    groups = defaultdict(list)
    for c in Company.query.with_entities(Company.id, Company.name).all():
        key = _re.sub(r'[^a-z0-9]', '',
                      (c[1] or '').lower()
                      .replace('limited', 'ltd')
                      .replace('private', 'pvt'))
        if key:
            groups[key].append(c[1])

    dupes = [(k, names) for k, names in groups.items() if len(names) > 1]
    if not dupes:
        return Result(headline='No duplicate account names found.',
                      empty=True, sources=['companies.name'])
    rows = [{'Looks like one account': ' · '.join(sorted(set(names))),
             'Records': len(names)}
            for _k, names in sorted(dupes, key=lambda kv: -len(kv[1]))]
    capped, notes = _cap(rows, len(rows))
    notes.append('Matched on the name alone. The Data Mapping screen '
                 'decides and merges.')
    return Result(headline=f'{len(dupes)} possible duplicate account(s).',
                  columns=['Looks like one account', 'Records'],
                  rows=capped, figures={'groups': len(dupes)}, notes=notes,
                  sources=['companies.name', 'Data Mapping'])


# ── §5 capabilities that had no intent ───────────────────────────────
@intent('my_performance', 'My numbers',
        params={'days': 'window in days (default 90)'},
        personas=('sales', 'head'), phase=2,
        examples=('how am I doing', 'my numbers', 'my performance',
                  'what have I booked'))
def my_performance(scope, params):
    from app import Lead, Opportunity

    days = int(params.get('days') or 90)
    since = _days_ago(days)
    leads_in = sc_mod.leads(sc=scope).filter(Lead.created_at >= since).count()
    opps = sc_mod.opportunities(sc=scope).all()
    won = [o for o in opps if o.stage == 'Won' and (o.won_at or since) >= since]
    lost = [o for o in opps if o.stage == 'Lost']
    open_now = [o for o in opps if (o.stage or '') not in _TERMINAL]

    figures = {
        'new_leads': leads_in, 'won': len(won),
        'won_value': sum(float(o.value_inr or 0) for o in won),
        'lost': len(lost), 'open': len(open_now),
        'open_value': sum(float(o.value_inr or 0) for o in open_now),
        'window_days': days,
    }
    return Result(
        headline=(f'Last {days} days — {leads_in} new lead(s), {len(won)} '
                  f'won ({_money(figures["won_value"])}), {len(open_now)} '
                  f'still open ({_money(figures["open_value"])}).'),
        figures=figures,
        sources=['leads.created_at', 'opportunities.stage',
                 'opportunities.won_at'])


@intent('closing_this_month', 'Likely to close this month',
        permission='module.funnels', personas=('sales', 'head', 'mgmt'),
        phase=2,
        examples=('what is likely to close this month',
                  'likely bookings', 'closing this month'))
def closing_this_month(scope, params):
    from datetime import date

    from app import Opportunity

    today = date.today()
    start = date(today.year, today.month, 1)
    if today.month == 12:
        end = date(today.year + 1, 1, 1)
    else:
        end = date(today.year, today.month + 1, 1)

    # The lower bound is the whole point, and it was missing: without it
    # every close date in the CRM's history counted as "this month", and
    # production answered 355 of 377 open opportunities. An overdue date
    # is worth knowing about, but it is a different question and gets
    # its own line rather than being folded into the forecast.
    overdue = (sc_mod.opportunities(sc=scope)
               .filter(~Opportunity.stage.in_(_TERMINAL),
                       Opportunity.expected_close_date.isnot(None),
                       Opportunity.expected_close_date < start).count())

    found = (sc_mod.opportunities(sc=scope)
             .filter(~Opportunity.stage.in_(_TERMINAL),
                     Opportunity.expected_close_date.isnot(None),
                     Opportunity.expected_close_date >= start,
                     Opportunity.expected_close_date < end)
             .order_by(Opportunity.value_inr.desc().nullslast()).all())
    if not found:
        no_date = (sc_mod.opportunities(sc=scope)
                   .filter(~Opportunity.stage.in_(_TERMINAL),
                           Opportunity.expected_close_date.is_(None))
                   .count())
        notes = []
        if overdue:
            notes.append(f'{overdue} open opportunity(s) have a close date '
                         f'that has already passed — worth re-dating.')
        if no_date:
            notes.append(f'{no_date} open opportunities have no expected '
                         f'close date at all, so this cannot see them.')
        return Result(
            headline='Nothing is dated to close in the rest of this month.',
            empty=True, notes=notes, figures={'overdue': overdue,
                                              'no_date': no_date},
            sources=['opportunities.expected_close_date'])

    rows = [{'Opportunity': o.opp_number, 'Value': _money(o.value_inr),
             'Probability': f'{o.probability or 0}%', 'Stage': o.stage,
             'Close': str(o.expected_close_date or '')[:10],
             'Owner': o.owner_emp_code or '—',
             '_chip': {'type': 'opportunity', 'id': o.id,
                       'label': o.opp_number}} for o in found]
    capped, notes = _cap(rows, len(rows))
    weighted = sum(float(o.value_inr or 0) * (o.probability or 0) / 100.0
                   for o in found)
    if overdue:
        notes.append(f'Separately, {overdue} open opportunity(s) have a '
                     f'close date that has already passed.')
    return Result(
        headline=(f'{len(found)} opportunity(s) dated to close in '
                  f'{today.strftime("%B")}, {_money(weighted)} weighted.'),
        columns=['Opportunity', 'Value', 'Probability', 'Stage', 'Close',
                 'Owner'],
        rows=capped,
        figures={'count': len(found), 'weighted': weighted,
                 'overdue': overdue},
        notes=notes, sources=['opportunities.expected_close_date',
                              'opportunities.probability'])


# ══════════════════════════════════════════════════════════════════════
#  §4 — RETRIEVAL OVER THE UNSTRUCTURED HALF
# ══════════════════════════════════════════════════════════════════════

@intent('search_text', 'Search the notes and emails',
        params={'term': 'what to look for in the text'},
        personas=('sales', 'head', 'mgmt', 'ops'), phase=4,
        examples=('what did anyone say about the Airoli transformer',
                  'search the emails for hydraulic axle',
                  'find mentions of demurrage',
                  'anything about the Kandla job'))
def search_text(scope, params):
    """The half of the CRM that is not a column.

    Permission is applied to the candidate set before ranking, so a
    chunk the viewer may not see is never scored, never returned, and
    never reaches a model.
    """
    from app.copilot import retrieval

    term, filters = _text_filters(params)
    if len(term) < 3:
        return Result(headline='Give me a few words to search the text for.',
                      empty=True)

    found = retrieval.search(scope, term, limit=10, **filters)
    hits = found['hits']
    if not hits:
        index = retrieval.stats()
        if not index.get('chunks'):
            return Result(
                headline='The text index has not been built yet.',
                empty=True,
                notes=['An administrator builds it with '
                       'scripts/build_copilot_index.py. Until then this '
                       'searches nothing — which is why it says so '
                       'rather than reporting no matches.'],
                sources=['copilot_chunk'])
        return Result(headline=f'Nothing in the notes or emails you can '
                               f'see mentions "{term}".',
                      empty=True, sources=['copilot_chunk'])

    rows = [{'Account': h['account'] or '—',
             'Where': h['source'],
             'When': h['when'] or '—',
             'Text': h['text'][:300],
             '_chip': {'type': 'lead', 'id': h['lead_id'],
                       'label': h['account'] or f'Lead {h["lead_id"]}'}}
            for h in hits]
    notes = []
    if found['backend'] == 'lexical':
        notes.append('Matched on wording. With an internal embedding '
                     'model configured this would match on meaning too.')
    if found.get('expanded'):
        notes.append('Also matched: ' + ', '.join(found['expanded'][:6])
                     + '.')
    notes.append('Ranked by how closely the words match (phrase and '
                 'nearness count), then the kind of text — enquiry and '
                 'notes above emails, signature blocks down — and '
                 f'recency, halving every {retrieval.RECENCY_HALF_LIFE_DAYS}'
                 ' days.')
    return Result(
        headline=f'{len(hits)} passage(s) mentioning "{term}".',
        columns=['Account', 'Where', 'When', 'Text'], rows=rows,
        figures={'hits': len(hits), 'backend': found['backend']},
        notes=notes,
        citations=[h['citation'] for h in hits],
        filters=found.get('filters') or {},
        sources=['lead_emails', 'lead_notes', 'leads.original_email_body'])


#: "in the notes", "in emails", "in the enquiry" — where to look.
_SOURCE_PHRASE = re.compile(
    r'\b(?:in|from|within)\s+(?:the\s+)?(notes?|e-?mails?|enquir(?:y|ies)|'
    r'inquir(?:y|ies))\b', re.IGNORECASE)
#: "last 30 days", "past 2 weeks", "this month", "since 2026-08-01".
_WINDOW_PHRASE = re.compile(
    r'\b(?:in\s+the\s+)?(?:last|past)\s+(\d+)\s*(days?|weeks?|months?)\b|'
    r'\b(this|last)\s+(week|month|quarter|year)\b', re.IGNORECASE)
_SINCE_PHRASE = re.compile(r'\bsince\s+(\d{4}-\d{2}-\d{2})\b', re.IGNORECASE)


def _text_filters(params):
    """(term, retrieval filters) from the params and the words.

    Filters the classifier (or a model) supplied are used as given; the
    same filters written into the question — "in the notes", "last 30
    days" — are read out and removed from the search words, so "notes"
    is not itself searched for.
    """
    import re as _re
    from datetime import date, timedelta as _td

    term = (params.get('term') or '').strip()
    out = {}
    if params.get('lead_id'):
        out['lead_id'] = params['lead_id']
    ctx = params.get('context') or {}
    if params.get('company_id') or (ctx.get('type') in ('company', 'account')
                                    and ctx.get('id')):
        out['company_id'] = params.get('company_id') or ctx.get('id')
    for key in ('owner', 'date_from', 'date_to'):
        if params.get(key):
            out[key] = params[key]
    source = str(params.get('source') or '').lower()
    m = _SOURCE_PHRASE.search(term)
    if m:
        word = m.group(1).lower()
        source = source or ('note' if word.startswith('note') else
                            'email' if 'mail' in word else 'enquiry')
        term = (term[:m.start()] + term[m.end():]).strip(' ,-')
    if source in retrieval_source_types():
        out['source'] = source
    days = params.get('days')
    m = _WINDOW_PHRASE.search(term)
    if m:
        if m.group(1):
            unit = m.group(2).lower()
            days = int(m.group(1)) * (7 if unit.startswith('week') else
                                      30 if unit.startswith('month') else 1)
        else:
            days = {'week': 7, 'month': 30, 'quarter': 90,
                    'year': 365}[m.group(4).lower()]
        term = (term[:m.start()] + term[m.end():]).strip(' ,-')
    m = _SINCE_PHRASE.search(term)
    if m:
        out['date_from'] = m.group(1)
        term = (term[:m.start()] + term[m.end():]).strip(' ,-')
    if days and not out.get('date_from'):
        try:
            out['date_from'] = (date.today() - _td(days=int(days))).isoformat()
        except (TypeError, ValueError):
            pass
    term = _re.sub(r'\s{2,}', ' ', term).strip()
    return term, out


def retrieval_source_types():
    from app.copilot import retrieval
    return retrieval.SOURCE_TYPES


# ══════════════════════════════════════════════════════════════════════
#  PENDING FOLLOW-UPS — the list behind My Day's counts
# ══════════════════════════════════════════════════════════════════════
@intent('followups_due', 'Follow-ups due',
        params={'days': 'include follow-ups due within this many days '
                        '(default 0: overdue and today)'},
        personas=('sales', 'head'), phase=1,
        examples=('which follow-ups are due', 'pending follow-ups',
                  'my overdue follow-ups', 'follow ups due this week'))
def followups_due(scope, params):
    """My Day says "12 overdue". This is the twelve.

    Ordered most overdue first, because that is the order to work them
    in. Reads the same followup_date My Day and the tiles count, so the
    list and the number cannot disagree.
    """
    from datetime import date, timedelta as _td

    from app import Lead

    ahead = max(int(params.get('days') or 0), 0)
    today = date.today()
    horizon = today + _td(days=ahead)

    base = (sc_mod.leads(sc=scope)
            .filter(~Lead.stage.in_(_TERMINAL),
                    Lead.followup_date.isnot(None),
                    Lead.followup_date <= horizon))
    # Counted in SQL and only the displayed slice loaded — the same
    # figures, without reading thousands of rows to show fifty.
    total = base.count()
    if not total:
        return Result(
            headline=('No follow-ups are due.' if not ahead else
                      f'No follow-ups due in the next {ahead} days.'),
            empty=True, sources=['leads.followup_date'])

    overdue = base.filter(Lead.followup_date < today).count()
    found = (base.with_entities(Lead.id, Lead.company, Lead.stage,
                                Lead.followup_date, Lead.assigned_to)
             .order_by(Lead.followup_date.asc(), Lead.id.asc())
             .limit(ROW_CAP).all())
    rows = []
    for l in found[:ROW_CAP]:
        late = (today - l.followup_date).days
        rows.append({
            'Company': l.company, 'Stage': l.stage,
            'Due': str(l.followup_date),
            'Status': (f'{late} day(s) overdue' if late > 0
                       else 'today' if late == 0
                       else f'in {-late} day(s)'),
            'Owner': l.assigned_to or '—',
            '_chip': _lead_chip(l)})
    notes = ([f'Showing the first {ROW_CAP} of {total}.']
             if total > ROW_CAP else [])
    return Result(
        headline=(f'{total} follow-up(s) due — {overdue} overdue.'),
        columns=['Company', 'Stage', 'Due', 'Status', 'Owner'],
        rows=rows, figures={'count': total, 'overdue': overdue},
        notes=notes, sources=['leads.followup_date'])
