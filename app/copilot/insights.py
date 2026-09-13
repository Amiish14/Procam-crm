"""
The second shelf of approved queries — health, risk, relationships,
the deal documents' queues, and the performance and admin questions.

Everything in queries.py's module docstring holds here without
exception: each handler takes an already-resolved Scope, reaches the
database only through app.access.scope / app.access.records (or a
service that already defines the rule, like task_engine's My Work),
returns a Result, and says "nothing recorded" rather than inventing a
number. It lives in its own file only so neither file becomes a
three-thousand-line scroll.

The health and risk intents add one rule of their own
    Every score is a sum of named factors with fixed points, written as
    constants below and restated in the answer's notes. No weights are
    learned, nothing is opaque, and a person reading the answer can
    recompute the score by hand — which is the only kind of score a
    salesperson should be asked to act on.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, or_

from app.access import records as rec_mod
from app.access import scope as sc_mod
from app.copilot.intents import Result, intent
from app.copilot.queries import (
    ROW_CAP, MIN_SAMPLE_FOR_ANALYSIS, _TERMINAL, _age, _cap, _clarify,
    _days_ago, _lead_chip, _money, _narrow_leads_to_service,
    _narrow_opps_to_service, _nothing_recorded, _now, _pick_company,
    _resolve_lead, _service_filters, _service_param)

#: Opportunity stages that are decided. The Opportunity screen writes
#: "Closed Won"/"Closed Lost"; the older paths and the Copilot's first
#: intents wrote "Won"/"Lost". Both spellings are read as one outcome.
WON = ('Won', 'Closed Won')
LOST = ('Lost', 'Closed Lost')
_OPP_CLOSED = tuple(set(_TERMINAL) | set(WON) | set(LOST))

VERT = 'only this service, e.g. Project Freight'


def _int(value, default):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _company_chip(c):
    return {'type': 'company', 'id': c.id, 'label': c.name}


def _opp_chip(o):
    return {'type': 'opportunity', 'id': o.id, 'label': o.opp_number}


def _names(codes):
    """emp_code → display name, for the codes given. Names of colleagues
    are not restricted data; figures about them are, and none are read
    here."""
    from app import Employee

    codes = [c for c in set(codes) if c]
    if not codes:
        return {}
    return {e.emp_code: (e.name or e.emp_code) for e in
            Employee.query.filter(Employee.emp_code.in_(codes)).all()}


def _routing_only(company):
    """§6.6's safe form for an account the viewer may not see into:
    that it exists, who owns it, and nothing else."""
    who = _names([company.pic_emp_code]).get(company.pic_emp_code) \
        if company.pic_emp_code else None
    head = (f'{company.name} is outside your CRM access. '
            + (f'Its owner is {who} — ask them for detail.' if who else
               'Nobody owns it yet; an administrator can assign it.'))
    return Result(headline=head, restricted=True, empty=True,
                  sources=['Account Master'])


def _account_subject(scope, params, ask):
    """(company, early Result) — the account a question is about, with
    the clarification, "which account?" and "not found" answers already
    made. `early` is None when the caller should carry on."""
    company, clarification = _pick_company(scope, params, ask=ask)
    if clarification is not None:
        return None, clarification
    name = (params.get('account') or '').strip()
    if company is None:
        if name:
            return None, Result(
                headline=f'No account matching "{name}" in the CRM.',
                empty=True, sources=['companies.name'])
        return None, _which_account(scope, ask)
    return company, None


def _which_account(scope, ask):
    """"Which account?" — offered as the viewer's own busiest accounts,
    so the options are ones they can actually open."""
    from app import Company

    options = (sc_mod.companies(sc=scope)
               .filter(Company.is_active.is_(True))
               .order_by(Company.last_activity_at.desc().nullslast(),
                         Company.id.asc())
               .limit(3).all())
    if not options:
        return Result(headline='Which account? Name the customer.',
                      empty=True)
    return _clarify('Which account?', [(c.name, ask(c.name))
                                       for c in options])


# ══════════════════════════════════════════════════════════════════════
#  QUOTES AND RFQs — the document queues
# ══════════════════════════════════════════════════════════════════════
@intent('quotes_pending_approval', 'Quotes pending approval',
        permission='module.quotes', personas=('head', 'sales', 'mgmt'),
        phase=2,
        examples=('quotes pending approval', 'which quotes need approval',
                  'quotations awaiting approval'))
def quotes_pending_approval(scope, params):
    from app.models.quote import Quote

    visible = rec_mod.quotes(sc=scope)
    if not visible.count():
        return _nothing_recorded('quotes',
                                 'The Quotes module has no records yet.')
    base = visible.filter(Quote.status == 'Awaiting Approval')
    total = base.count()
    drafts = visible.filter(Quote.status == 'Draft').count()
    if not total:
        return Result(headline='No quotes are waiting for approval.',
                      empty=True, figures={'drafts': drafts},
                      notes=([f'{drafts} quote(s) are still drafts.']
                             if drafts else []),
                      sources=['quotes.status'])
    found = (base.order_by(Quote.updated_at.asc().nullsfirst(),
                           Quote.id.asc()).limit(ROW_CAP).all())
    rows = [{'Quote': q.quote_number, 'Subject': q.subject or '—',
             'Value': _money(q.total_amount),
             'Waiting days': _age(q.updated_at or q.created_at) or 0,
             'Prepared by': q.prepared_by_id or '—',
             '_chip': {'type': 'quote', 'id': q.id,
                       'label': q.quote_number}} for q in found]
    rows, notes = _cap(rows, total)
    if drafts:
        notes.append(f'Separately, {drafts} quote(s) are still drafts.')
    return Result(
        headline=f'{total} quote(s) waiting for approval.',
        columns=['Quote', 'Subject', 'Value', 'Waiting days', 'Prepared by'],
        rows=rows, figures={'count': total, 'drafts': drafts}, notes=notes,
        sources=['quotes.status', 'quotes.updated_at'])


#: Quote statuses that are still live with the customer.
_LIVE_QUOTE = ('Approved', 'Submitted', 'Under Negotiation')


@intent('quotes_expiring', 'Quotes expiring soon',
        permission='module.quotes',
        params={'days': 'look ahead this many days (default 7)'},
        personas=('sales', 'head'), phase=2,
        examples=('quotes expiring this week', 'which quotes are about to '
                  'expire', 'quote validity ending soon'))
def quotes_expiring(scope, params):
    from app.models.quote import Quote

    visible = rec_mod.quotes(sc=scope)
    if not visible.count():
        return _nothing_recorded('quotes',
                                 'The Quotes module has no records yet.')
    days = _int(params.get('days'), 7)
    today = _now().date()
    base = visible.filter(Quote.status.in_(_LIVE_QUOTE),
                          Quote.validity_until.isnot(None),
                          Quote.validity_until <= today + timedelta(days=days))
    total = base.count()
    if not total:
        undated = visible.filter(Quote.status.in_(_LIVE_QUOTE),
                                 Quote.validity_until.is_(None)).count()
        return Result(
            headline=f'No live quote expires in the next {days} days.',
            empty=True, figures={'undated': undated},
            notes=([f'{undated} live quote(s) have no validity date, so '
                    f'this cannot see them.'] if undated else []),
            sources=['quotes.validity_until'])
    expired = base.filter(Quote.validity_until < today).count()
    found = (base.order_by(Quote.validity_until.asc(), Quote.id.asc())
             .limit(ROW_CAP).all())
    rows = []
    for q in found:
        left = (q.validity_until - today).days
        rows.append({'Quote': q.quote_number, 'Value': _money(q.total_amount),
                     'Status': q.status,
                     'Valid until': str(q.validity_until),
                     'Days left': left,
                     '_chip': {'type': 'quote', 'id': q.id,
                               'label': q.quote_number}})
    rows, notes = _cap(rows, total)
    return Result(
        headline=(f'{total} live quote(s) expire within {days} days'
                  + (f' — {expired} already past validity.' if expired
                     else '.')),
        columns=['Quote', 'Value', 'Status', 'Valid until', 'Days left'],
        rows=rows, figures={'count': total, 'expired': expired,
                            'days': days},
        notes=notes, sources=['quotes.validity_until', 'quotes.status'])


@intent('quotes_recent', 'Recent quotes',
        permission='module.quotes',
        params={'days': 'window in days (default 7)'},
        personas=('sales', 'head', 'mgmt'), phase=2,
        examples=('quotes sent this week', 'recent quotes',
                  'quotes issued in the last 30 days'))
def quotes_recent(scope, params):
    from app.models.quote import Quote

    visible = rec_mod.quotes(sc=scope)
    if not visible.count():
        return _nothing_recorded('quotes',
                                 'The Quotes module has no records yet.')
    days = _int(params.get('days'), 7)
    base = visible.filter(Quote.quote_date >= _days_ago(days).date())
    total = base.count()
    if not total:
        return Result(headline=f'No quotes dated in the last {days} days.',
                      empty=True, sources=['quotes.quote_date'])
    value = sum(float(v or 0) for (v,) in
                base.with_entities(Quote.total_amount).all())
    found = (base.order_by(Quote.quote_date.desc(), Quote.id.desc())
             .limit(ROW_CAP).all())
    rows = [{'Quote': q.quote_number, 'Subject': q.subject or '—',
             'Value': _money(q.total_amount), 'Status': q.status,
             'Date': str(q.quote_date or '')[:10],
             '_chip': {'type': 'quote', 'id': q.id,
                       'label': q.quote_number}} for q in found]
    rows, notes = _cap(rows, total)
    return Result(
        headline=(f'{total} quote(s) in the last {days} days, '
                  f'{_money(value)} in total.'),
        columns=['Quote', 'Subject', 'Value', 'Status', 'Date'], rows=rows,
        figures={'count': total, 'value': value, 'days': days},
        notes=notes, sources=['quotes.quote_date', 'quotes.total_amount'])


@intent('quotes_by_status', 'Quotes by status',
        permission='module.quotes', personas=('head', 'mgmt'), phase=2,
        examples=('quotes by status', 'quote status summary',
                  'status of all quotes'))
def quotes_by_status(scope, params):
    from app.models.quote import Quote

    visible = rec_mod.quotes(sc=scope)
    rows = (visible.with_entities(Quote.status, func.count(Quote.id))
            .group_by(Quote.status).all())
    if not rows:
        return _nothing_recorded('quotes',
                                 'The Quotes module has no records yet.')
    values = {}
    for status, amount in visible.with_entities(Quote.status,
                                                Quote.total_amount).all():
        values[status] = values.get(status, 0.0) + float(amount or 0)
    out = [{'Status': s or '—', 'Quotes': n, 'Value': _money(values.get(s))}
           for s, n in sorted(rows, key=lambda r: (-r[1], r[0] or ''))]
    total = sum(n for _s, n in rows)
    return Result(headline=f'{total} quote(s) across {len(out)} status(es).',
                  columns=['Status', 'Quotes', 'Value'], rows=out,
                  figures={'count': total}, sources=['quotes.status'])


@intent('rfqs_recent', 'Recent RFQs',
        permission='module.rfq',
        params={'days': 'window in days (default 7)'},
        personas=('sales', 'head', 'mgmt'), phase=2,
        examples=('RFQs received this week', 'recent RFQs',
                  'RFQs that came in today'))
def rfqs_recent(scope, params):
    from app.models.rfq import RFQ

    visible = rec_mod.rfqs(sc=scope)
    if not visible.count():
        return _nothing_recorded('RFQs', 'The RFQ module has no records yet.')
    days = _int(params.get('days'), 7)
    base = visible.filter(RFQ.received_date >= _days_ago(days).date())
    total = base.count()
    if not total:
        return Result(headline=f'No RFQs received in the last {days} days.',
                      empty=True, sources=['rfqs.received_date'])
    found = (base.order_by(RFQ.received_date.desc(), RFQ.id.desc())
             .limit(ROW_CAP).all())
    rows = [{'RFQ': r.rfq_number, 'Subject': r.subject or '—',
             'Received': str(r.received_date or '')[:10],
             'Quote by': str(r.quote_by_date or '')[:10] or '—',
             'Status': r.status or '—', 'Owner': r.lead_driver or '—',
             '_chip': {'type': 'rfq', 'id': r.id, 'label': r.rfq_number}}
            for r in found]
    rows, notes = _cap(rows, total)
    return Result(
        headline=f'{total} RFQ(s) received in the last {days} days.',
        columns=['RFQ', 'Subject', 'Received', 'Quote by', 'Status', 'Owner'],
        rows=rows, figures={'count': total, 'days': days}, notes=notes,
        sources=['rfqs.received_date'])


@intent('rfqs_by_status', 'RFQs by status',
        permission='module.rfq', personas=('head', 'mgmt'), phase=2,
        examples=('RFQs by status', 'RFQ status summary',
                  'status of all RFQs'))
def rfqs_by_status(scope, params):
    from app.models.rfq import RFQ

    rows = (rec_mod.rfqs(sc=scope)
            .with_entities(RFQ.status, func.count(RFQ.id))
            .group_by(RFQ.status).all())
    if not rows:
        return _nothing_recorded('RFQs', 'The RFQ module has no records yet.')
    out = [{'Status': s or '—', 'RFQs': n}
           for s, n in sorted(rows, key=lambda r: (-r[1], r[0] or ''))]
    total = sum(n for _s, n in rows)
    return Result(headline=f'{total} RFQ(s) across {len(out)} status(es).',
                  columns=['Status', 'RFQs'], rows=out,
                  figures={'count': total}, sources=['rfqs.status'])


# ══════════════════════════════════════════════════════════════════════
#  HANDOVERS
# ══════════════════════════════════════════════════════════════════════
@intent('handovers_awaiting_po', 'Won deals awaiting PO or handover',
        permission='module.handovers',
        params={'stage': "'po' for awaiting PO only, 'handover' for PO "
                         "captured but not handed over"},
        personas=('ops', 'mgmt', 'sales'), phase=2,
        examples=('won deals awaiting PO', 'pending handovers',
                  'handovers waiting for a purchase order'))
def handovers_awaiting_po(scope, params):
    from app.models.tms_handover import HandoverStatus, WonHandover

    visible = rec_mod.handovers(sc=scope)
    if not visible.count():
        return _nothing_recorded('handovers')
    stage = str(params.get('stage') or '').lower()
    wanted = {'po': (HandoverStatus.AWAITING_PO,),
              'handover': (HandoverStatus.PENDING,
                           HandoverStatus.PROJECT_MADE)}.get(
        stage, (HandoverStatus.AWAITING_PO, HandoverStatus.PENDING,
                HandoverStatus.PROJECT_MADE))
    base = visible.filter(WonHandover.status.in_(wanted))
    total = base.count()
    if not total:
        return Result(headline='Nothing is waiting on a PO or a handover.',
                      empty=True, sources=['won_handovers.status'])
    counts = dict(base.with_entities(WonHandover.status,
                                     func.count(WonHandover.id))
                  .group_by(WonHandover.status).all())
    found = (base.order_by(WonHandover.created_at.asc(),
                           WonHandover.id.asc()).limit(ROW_CAP).all())
    rows = [{'Account': h.account_name or '—', 'Value': _money(h.won_value),
             'Status': h.status or '—',
             'Days waiting': _age(h.created_at) or 0,
             'PIC': h.pic_emp_code or '—',
             '_chip': {'type': 'handover', 'id': h.id,
                       'label': h.account_name or f'Handover {h.id}'}}
            for h in found]
    rows, notes = _cap(rows, total)
    parts = ' · '.join(f'{n} {s.lower()}' for s, n in sorted(counts.items()))
    return Result(
        headline=f'{total} won deal(s) not yet handed over — {parts}.',
        columns=['Account', 'Value', 'Status', 'Days waiting', 'PIC'],
        rows=rows, figures={'count': total, **{
            k.lower().replace(' ', '_'): v for k, v in counts.items()}},
        notes=notes, sources=['won_handovers.status',
                              'won_handovers.created_at'])


# ══════════════════════════════════════════════════════════════════════
#  HEALTH AND RISK — transparent, additive scores
# ══════════════════════════════════════════════════════════════════════
#: Account health: six factors, 100 points. Recomputable by hand.
ACCOUNT_HEALTH_POINTS = {
    'recent_contact': 30,   # contact within 30 days (15 within 90)
    'open_pipeline': 20,    # at least one open opportunity
    'won_business': 20,     # won in the last 365 days (10 if ever)
    'next_action': 15,      # a follow-up or next action dated today or later
    'owner': 10,            # a primary owner is set
    'contacts': 5,          # at least one contact on record
}
ACCOUNT_HEALTHY = 70
ACCOUNT_WATCH = 40
_ACCOUNT_NOTE = ('Health = recent contact (30 if within 30 days, 15 within '
                 '90) + open pipeline (20) + won business (20 in the last '
                 'year, 10 if ever) + a next action dated today or later '
                 '(15) + an owner (10) + a contact on record (5). 70+ '
                 'healthy, 40–69 watch, below 40 at risk. Counted only over '
                 'records you can see.')


def _band(score):
    if score >= ACCOUNT_HEALTHY:
        return 'healthy'
    if score >= ACCOUNT_WATCH:
        return 'watch'
    return 'at risk'


def _account_factors(company, *, last_contact, open_opps, open_value,
                     won_recent, won_ever, next_action, overdue,
                     contacts, full):
    """[(factor, points, max, evidence)] for one account."""
    P = ACCOUNT_HEALTH_POINTS
    out = []
    days = _age(last_contact) if last_contact else None
    if days is not None and days <= 30:
        out.append(('Recent contact', P['recent_contact'],
                    P['recent_contact'], f'last contact {days} day(s) ago'))
    elif days is not None and days <= 90:
        out.append(('Recent contact', P['recent_contact'] // 2,
                    P['recent_contact'], f'last contact {days} days ago'))
    else:
        out.append(('Recent contact', 0, P['recent_contact'],
                    f'last contact {days} days ago' if days is not None
                    else 'no contact recorded'))
    out.append(('Open pipeline', P['open_pipeline'] if open_opps else 0,
                P['open_pipeline'],
                f'{open_opps} open · {_money(open_value)}' if open_opps
                else 'no open opportunity'))
    out.append(('Won business',
                P['won_business'] if won_recent else
                P['won_business'] // 2 if won_ever else 0,
                P['won_business'],
                f'{won_recent} won in the last year' if won_recent else
                f'{won_ever} won, none in the last year' if won_ever else
                'nothing won'))
    out.append(('Next action', P['next_action'] if next_action else 0,
                P['next_action'],
                'a next action is dated' if next_action else
                (f'{overdue} follow-up(s) overdue' if overdue else
                 'no next action set')))
    out.append(('Owner', P['owner'] if company.pic_emp_code else 0,
                P['owner'], 'owner set' if company.pic_emp_code
                else 'no owner'))
    out.append(('Contacts', P['contacts'] if contacts else 0, P['contacts'],
                f'{contacts} contact(s)' if contacts else 'no contact on '
                                                          'record'))
    if not full:
        out.append(('(partial view)', 0, 0,
                    'another person owns this account; only your own leads '
                    'and opportunities on it are counted'))
    return out


def _account_evidence(scope, company_ids, *, full_ids):
    """Per-account inputs for the health score, from scoped queries only.

    One pass per table rather than a query per account, so a list of a
    few hundred accounts stays a handful of reads.
    """
    from app import Company, Lead, Opportunity
    from app.services import contact as _contact

    ids = list(company_ids)
    ev = {cid: {'last_contact': None, 'open_opps': 0, 'open_value': 0.0,
                'won_recent': 0, 'won_ever': 0, 'next_action': False,
                'overdue': 0, 'contacts': 0} for cid in ids}
    if not ids:
        return ev
    today = _now().date()
    year_ago = _days_ago(365)
    for chunk in [ids[i:i + 500] for i in range(0, len(ids), 500)]:
        for cid, stage, value, won_at in (
                sc_mod.opportunities(sc=scope)
                .filter(Opportunity.company_id.in_(chunk))
                .with_entities(Opportunity.company_id, Opportunity.stage,
                               Opportunity.value_inr, Opportunity.won_at)
                .all()):
            e = ev[cid]
            if stage in WON:
                e['won_ever'] += 1
                if won_at and won_at >= year_ago:
                    e['won_recent'] += 1
            elif stage not in _OPP_CLOSED:
                e['open_opps'] += 1
                e['open_value'] += float(value or 0)
        leads = (sc_mod.leads(sc=scope).filter(Lead.company_id.in_(chunk))
                 .with_entities(Lead.id, Lead.company_id, Lead.stage,
                                Lead.followup_date).all())
        seen = _contact.last_contacts([l.id for l in leads])
        for l in leads:
            e = ev[l.company_id]
            stamp = seen.get(l.id)
            if stamp and (e['last_contact'] is None
                          or stamp > e['last_contact']):
                e['last_contact'] = stamp
            if (l.stage or '') not in _TERMINAL and l.followup_date:
                if l.followup_date >= today:
                    e['next_action'] = True
                else:
                    e['overdue'] += 1
        for cid, n in (sc_mod.contacts(sc=scope)
                       .filter(_contact_company_col().in_(chunk))
                       .with_entities(_contact_company_col(),
                                      func.count())
                       .group_by(_contact_company_col()).all()):
            if cid in ev:
                ev[cid]['contacts'] = n
        # The account's own dates count only where the whole account is
        # the viewer's: they summarise everyone's work on it.
        own = [c for c in chunk if c in full_ids]
        if own:
            for cid, last, nxt in (sc_mod.companies(sc=scope)
                                   .filter(Company.id.in_(own))
                                   .with_entities(Company.id,
                                                  Company.last_activity_at,
                                                  Company.next_action_at)
                                   .all()):
                e = ev[cid]
                if last and (e['last_contact'] is None
                             or last > e['last_contact']):
                    e['last_contact'] = last
                if nxt and nxt >= today:
                    e['next_action'] = True
    return ev


def _contact_company_col():
    from app import Contact
    return Contact.company_id


@intent('account_health', 'Account health',
        params={'account': 'the customer name (omit for a ranked list)',
                'account_id': 'or the account id',
                'segment': "'customers' for accounts with won business"},
        personas=('sales', 'head', 'mgmt'), phase=3,
        examples=('account health for Tata Steel', 'which accounts are at '
                  'risk', 'customer health'))
def account_health(scope, params):
    """One account's score with its factors, or a ranked list of the
    viewer's accounts, weakest first."""
    from app import Company

    if params.get('account') or params.get('account_id') or \
            (params.get('context') or {}).get('type') in ('company',
                                                          'account'):
        company, early = _account_subject(
            scope, params, lambda n: f'account health for {n}')
        if early is not None:
            return early
        access = rec_mod.company_access(company, sc=scope)
        if access == rec_mod.ROUTING:
            return _routing_only(company)
        full = access == rec_mod.FULL
        ev = _account_evidence(scope, [company.id],
                               full_ids={company.id} if full else set())
        factors = _account_factors(company, full=full, **ev[company.id])
        score = sum(p for _f, p, _m, _e in factors)
        weakest = [f for f, p, m, _e in factors if m and p < m]
        return Result(
            headline=(f'{company.name} — health {score}/100 '
                      f'({_band(score)})'
                      + (f'; weakest: {", ".join(weakest[:2]).lower()}.'
                         if weakest else '.')),
            columns=['Factor', 'Points', 'Out of', 'Evidence'],
            rows=[{'Factor': f, 'Points': p, 'Out of': m, 'Evidence': e,
                   '_chip': _company_chip(company)}
                  for f, p, m, e in factors],
            figures={'score': score, 'band': _band(score)},
            notes=[_ACCOUNT_NOTE], citations=[_company_chip(company)],
            filters={'account': company.name},
            sources=['opportunities', 'leads.followup_date', 'lead_emails',
                     'lead_activities', 'contacts', 'Account Master'])

    customers = str(params.get('segment') or '').lower().startswith('cust')
    from app import Lead, Opportunity
    owned = (sc_mod.companies(sc=scope).filter(Company.is_active.is_(True))
             .with_entities(Company.id))
    if scope.unrestricted:
        # Everyone's accounts is thousands of dormant names; the question
        # is about relationships, so only accounts with work on them.
        linked = {r[0] for r in sc_mod.opportunities(sc=scope)
                  .with_entities(Opportunity.company_id).distinct() if r[0]}
        linked |= {r[0] for r in sc_mod.leads(sc=scope)
                   .filter(~Lead.stage.in_(_TERMINAL))
                   .with_entities(Lead.company_id).distinct() if r[0]}
        ids = [r[0] for r in owned.filter(Company.id.in_(list(linked)[:5000]))
               .limit(500).all()] if linked else []
    else:
        ids = [r[0] for r in owned.limit(500).all()]
    if not ids:
        return Result(headline='No accounts in your scope to score.',
                      empty=True, notes=[_ACCOUNT_NOTE],
                      sources=['Account Master'])
    ev = _account_evidence(scope, ids, full_ids=set(ids))
    if customers:
        ids = [i for i in ids if ev[i]['won_ever']]
        if not ids:
            return Result(headline='No accounts in your scope have won '
                                   'business to score.', empty=True,
                          notes=[_ACCOUNT_NOTE],
                          sources=['opportunities.stage'])
    companies = {c.id: c for c in Company.query.filter(Company.id.in_(ids))}
    scored = []
    for cid in ids:
        c = companies.get(cid)
        if c is None:
            continue
        factors = _account_factors(c, full=True, **ev[cid])
        score = sum(p for _f, p, _m, _e in factors)
        weakest = min(((p / float(m), f) for f, p, m, _e in factors if m),
                      default=(1, '—'))[1]
        scored.append((score, c, weakest))
    scored.sort(key=lambda t: (t[0], t[1].name or ''))
    bands = {'at risk': 0, 'watch': 0, 'healthy': 0}
    for score, _c, _w in scored:
        bands[_band(score)] += 1
    names = _names(c.pic_emp_code for _s, c, _w in scored[:ROW_CAP])
    rows = [{'Account': c.name, 'Score': score, 'Band': _band(score),
             'Weakest factor': weakest,
             'Owner': names.get(c.pic_emp_code, c.pic_emp_code or '—'),
             '_chip': _company_chip(c)} for score, c, weakest in scored]
    rows, notes = _cap(rows, len(scored))
    notes.append(_ACCOUNT_NOTE)
    label = 'customer' if customers else 'account'
    return Result(
        headline=(f'{len(scored)} {label}(s) scored — {bands["at risk"]} at '
                  f'risk, {bands["watch"]} to watch, {bands["healthy"]} '
                  f'healthy.'),
        columns=['Account', 'Score', 'Band', 'Weakest factor', 'Owner'],
        rows=rows, figures={'count': len(scored), **{
            k.replace(' ', '_'): v for k, v in bands.items()}},
        notes=notes, filters=({'segment': 'customers'} if customers else {}),
        sources=['opportunities', 'leads.followup_date', 'lead_emails',
                 'contacts', 'Account Master'])


#: Pipeline health: five checks, each ok / watch / risk on fixed
#: thresholds (share of open opportunities, or of open value).
PIPELINE_CHECKS = (
    # key, label, watch above, risk above
    ('no_close_date', 'No expected close date', 0.10, 0.30),
    ('close_passed', 'Close date already passed', 0.10, 0.25),
    ('stalled', 'Unchanged for 30+ days', 0.20, 0.40),
    ('no_owner', 'No owner', 0.0, 0.05),
    ('concentration', 'Largest deal share of open value', 0.25, 0.50),
)
PIPELINE_RISK_PENALTY = 20
PIPELINE_WATCH_PENALTY = 10


@intent('pipeline_health', 'Pipeline health',
        permission='module.funnels',
        params={'vertical': VERT},
        personas=('head', 'mgmt', 'sales'), phase=3,
        examples=('pipeline health', 'how healthy is my pipeline',
                  'pipeline hygiene check'))
def pipeline_health(scope, params):
    from app import Opportunity

    service = _service_param(params)
    q = sc_mod.opportunities(sc=scope).filter(
        ~Opportunity.stage.in_(_OPP_CLOSED))
    if service:
        q = _narrow_opps_to_service(q, service)
    opps = q.with_entities(Opportunity.value_inr,
                           Opportunity.expected_close_date,
                           Opportunity.updated_at,
                           Opportunity.owner_emp_code).all()
    if not opps:
        return Result(headline='No open opportunities in your scope.',
                      empty=True, filters=_service_filters(service),
                      sources=['opportunities.stage'])
    n = float(len(opps))
    today = _now().date()
    cutoff = _days_ago(30)
    total = sum(float(o.value_inr or 0) for o in opps)
    top = max(float(o.value_inr or 0) for o in opps)
    measured = {
        'no_close_date': sum(1 for o in opps if not o.expected_close_date) / n,
        'close_passed': sum(1 for o in opps if o.expected_close_date
                            and o.expected_close_date < today) / n,
        'stalled': sum(1 for o in opps if o.updated_at is None
                       or o.updated_at < cutoff) / n,
        'no_owner': sum(1 for o in opps if not o.owner_emp_code) / n,
        'concentration': (top / total) if total else 0.0,
    }
    score, rows, attention = 100, [], 0
    for key, label, watch, risk in PIPELINE_CHECKS:
        share = measured[key]
        status = ('risk' if share > risk else
                  'watch' if share > watch else 'ok')
        if status == 'risk':
            score -= PIPELINE_RISK_PENALTY
            attention += 1
        elif status == 'watch':
            score -= PIPELINE_WATCH_PENALTY
            attention += 1
        rows.append({'Check': label, 'Value': f'{round(100 * share)}%',
                     'Status': status,
                     'Threshold': f'watch above {round(100 * watch)}%, '
                                  f'risk above {round(100 * risk)}%'})
    score = max(score, 0)
    band = 'healthy' if score >= 80 else 'watch' if score >= 60 else 'at risk'
    return Result(
        headline=(f'Pipeline health {score}/100 ({band}) — {_money(total)} '
                  f'across {len(opps)} open opportunities; {attention} '
                  f'check(s) need attention.'),
        columns=['Check', 'Value', 'Status', 'Threshold'], rows=rows,
        figures={'score': score, 'band': band, 'count': len(opps),
                 'total': total},
        notes=[f'Health starts at 100 and loses {PIPELINE_RISK_PENALTY} for '
               f'each check at risk and {PIPELINE_WATCH_PENALTY} for each to '
               f'watch. 80+ healthy, 60–79 watch, below 60 at risk. Shares '
               f'are of open opportunities, except concentration, which is '
               f'the largest deal as a share of open value.'],
        filters=_service_filters(service),
        sources=['opportunities.expected_close_date',
                 'opportunities.updated_at', 'opportunities.owner_emp_code',
                 'opportunities.value_inr'])


#: Opportunity risk: additive points; 50+ high, 25–49 medium, <25 low.
OPP_RISK_POINTS = {
    'close_passed': 30,
    'no_close_date': 15,
    'stalled_30': 25,
    'stalled_14': 15,
    'low_probability': 10,   # probability at or below 25%
    'no_owner': 10,
    'high_value': 10,        # ₹1 crore or more at stake
}
OPP_RISK_HIGH = 50
OPP_RISK_MEDIUM = 25
_OPP_RISK_NOTE = ('Risk = close date passed 30 · no close date 15 · '
                  'unchanged 30+ days 25 (14–29 days 15) · probability 25% '
                  'or less 10 · no owner 10 · ₹1 crore or more at stake 10. '
                  '50+ high, 25–49 medium, below 25 low.')


def _opp_risk(o, today):
    """(score, [(factor, points)]) for one opportunity."""
    P = OPP_RISK_POINTS
    hits = []
    if o.expected_close_date and o.expected_close_date < today:
        hits.append((f'close date passed '
                     f'{(today - o.expected_close_date).days} day(s) ago',
                     P['close_passed']))
    elif not o.expected_close_date:
        hits.append(('no expected close date', P['no_close_date']))
    still = _age(o.updated_at) if o.updated_at else None
    if still is None or still >= 30:
        hits.append((f'unchanged {still} days' if still is not None
                     else 'never updated', P['stalled_30']))
    elif still >= 14:
        hits.append((f'unchanged {still} days', P['stalled_14']))
    if o.probability is not None and o.probability <= 25:
        hits.append((f'probability {o.probability}%', P['low_probability']))
    if not o.owner_emp_code:
        hits.append(('no owner', P['no_owner']))
    if float(o.value_inr or 0) >= 1e7:
        hits.append(('₹1 crore or more at stake', P['high_value']))
    return sum(p for _f, p in hits), hits


def _risk_band(score):
    return ('high' if score >= OPP_RISK_HIGH else
            'medium' if score >= OPP_RISK_MEDIUM else 'low')


@intent('opportunity_risk', 'Opportunities at risk',
        permission='module.funnels',
        params={'opportunity_id': 'one opportunity',
                'opportunity': 'or its number, e.g. OPP-0012',
                'vertical': VERT, 'limit': 'how many (default 20)'},
        personas=('head', 'mgmt', 'sales'), phase=3,
        examples=('which deals are at risk', 'opportunities at risk',
                  'risky opportunities'))
def opportunity_risk(scope, params):
    from app import Opportunity

    today = _now().date()
    ctx = params.get('context') or {}
    one_id = params.get('opportunity_id') or (
        ctx.get('id') if ctx.get('type') in ('opportunity', 'opp') else None)
    number = str(params.get('opportunity') or '').strip()
    if one_id or number:
        q = sc_mod.opportunities(sc=scope)
        try:
            o = (q.filter(Opportunity.id == int(one_id)).first() if one_id
                 else q.filter(func.lower(Opportunity.opp_number)
                               == number.lower()).first())
        except (TypeError, ValueError):
            o = None
        if o is None:
            return Result(headline='That opportunity is outside what you can '
                                   'see, or does not exist.',
                          restricted=True, empty=True,
                          sources=['opportunities'])
        score, hits = _opp_risk(o, today)
        facts = [{'Factor': f, 'Points': p, '_chip': _opp_chip(o)}
                 for f, p in hits] or [{'Factor': 'no risk factor applies',
                                        'Points': 0, '_chip': _opp_chip(o)}]
        steps = []
        for f, _p in hits:
            if f.startswith('close date passed') or f.startswith('no expected'):
                steps.append('Re-date the expected close.')
            elif f.startswith('unchanged') or f == 'never updated':
                steps.append('Contact the customer and record the outcome.')
            elif f.startswith('probability'):
                steps.append('Confirm the deal is still real, or close it.')
            elif f == 'no owner':
                steps.append('Assign an owner.')
        return Result(
            headline=(f'{o.opp_number} — {o.stage}, {_money(o.value_inr)}, '
                      f'risk {score} ({_risk_band(score)}).'),
            columns=['Factor', 'Points'], rows=facts,
            figures={'score': score, 'band': _risk_band(score)},
            notes=([f'Next: {" ".join(dict.fromkeys(steps))}'] if steps
                   else []) + [_OPP_RISK_NOTE],
            citations=[_opp_chip(o)], recommendation=bool(steps),
            sources=['opportunities.expected_close_date',
                     'opportunities.updated_at', 'opportunities.probability'])

    limit = min(_int(params.get('limit'), 20), ROW_CAP)
    service = _service_param(params)
    q = sc_mod.opportunities(sc=scope).filter(
        ~Opportunity.stage.in_(_OPP_CLOSED))
    if service:
        q = _narrow_opps_to_service(q, service)
    opps = q.all()
    if not opps:
        return Result(headline='No open opportunities in your scope.',
                      empty=True, filters=_service_filters(service),
                      sources=['opportunities.stage'])
    scored = []
    for o in opps:
        score, hits = _opp_risk(o, today)
        if score >= OPP_RISK_MEDIUM:
            scored.append((score, o, hits))
    if not scored:
        return Result(headline=f'None of {len(opps)} open opportunities '
                               f'scores medium or high risk.',
                      empty=True, notes=[_OPP_RISK_NOTE],
                      filters=_service_filters(service),
                      sources=['opportunities'])
    scored.sort(key=lambda t: (-t[0], -float(t[1].value_inr or 0), t[1].id))
    high = sum(1 for s, _o, _h in scored if s >= OPP_RISK_HIGH)
    rows = [{'Opportunity': o.opp_number, 'Value': _money(o.value_inr),
             'Stage': o.stage, 'Risk': score, 'Band': _risk_band(score),
             'Why': '; '.join(f for f, _p in hits),
             'Owner': o.owner_emp_code or '—', '_chip': _opp_chip(o)}
            for score, o, hits in scored[:limit]]
    notes = ([f'Showing the top {limit} of {len(scored)}.']
             if len(scored) > limit else [])
    notes.append(_OPP_RISK_NOTE)
    return Result(
        headline=(f'{len(scored)} of {len(opps)} open opportunities at risk '
                  f'— {high} high.'),
        columns=['Opportunity', 'Value', 'Stage', 'Risk', 'Band', 'Why',
                 'Owner'],
        rows=rows, figures={'count': len(scored), 'high': high,
                            'open': len(opps)},
        notes=notes, filters=_service_filters(service),
        sources=['opportunities.expected_close_date',
                 'opportunities.updated_at', 'opportunities.probability'])


@intent('opps_overdue_close', 'Opportunities past their close date',
        permission='module.funnels',
        params={'vertical': VERT},
        personas=('head', 'mgmt', 'sales'), phase=2,
        examples=('opportunities past their close date',
                  'deals with overdue close dates', 'close date passed'))
def opps_overdue_close(scope, params):
    from app import Opportunity

    today = _now().date()
    service = _service_param(params)
    base = sc_mod.opportunities(sc=scope).filter(
        ~Opportunity.stage.in_(_OPP_CLOSED),
        Opportunity.expected_close_date.isnot(None),
        Opportunity.expected_close_date < today)
    if service:
        base = _narrow_opps_to_service(base, service)
    total = base.count()
    if not total:
        return Result(headline='No open opportunity is past its close date.',
                      empty=True, filters=_service_filters(service),
                      sources=['opportunities.expected_close_date'])
    found = (base.order_by(Opportunity.expected_close_date.asc(),
                           Opportunity.id.asc()).limit(ROW_CAP).all())
    rows = [{'Opportunity': o.opp_number, 'Title': o.title or '—',
             'Value': _money(o.value_inr),
             'Close': str(o.expected_close_date),
             'Days overdue': (today - o.expected_close_date).days,
             'Owner': o.owner_emp_code or '—', '_chip': _opp_chip(o)}
            for o in found]
    rows, notes = _cap(rows, total)
    return Result(
        headline=f'{total} open opportunity(s) past their expected close date.',
        columns=['Opportunity', 'Title', 'Value', 'Close', 'Days overdue',
                 'Owner'], rows=rows, figures={'count': total},
        notes=notes, filters=_service_filters(service),
        sources=['opportunities.expected_close_date'])


# ══════════════════════════════════════════════════════════════════════
#  ACCOUNTS — pipeline, cross-sell, relationships, contacts
# ══════════════════════════════════════════════════════════════════════
@intent('account_pipeline', 'Open deals for an account',
        permission='module.funnels',
        params={'account': 'the customer name', 'account_id': 'or its id'},
        personas=('sales', 'head', 'mgmt'), phase=2,
        examples=('open deals for Tata Steel', 'pipeline for JSW',
                  'opportunities with Godrej'))
def account_pipeline(scope, params):
    from app import Opportunity

    company, early = _account_subject(scope, params,
                                      lambda n: f'pipeline for {n}')
    if early is not None:
        return early
    opps = (sc_mod.opportunities(sc=scope)
            .filter(Opportunity.company_id == company.id,
                    ~Opportunity.stage.in_(_OPP_CLOSED))
            .order_by(Opportunity.value_inr.desc().nullslast(),
                      Opportunity.id.asc()).all())
    if not opps:
        if rec_mod.company_access(company, sc=scope) == rec_mod.ROUTING:
            return _routing_only(company)
        return Result(headline=f'No open opportunities with {company.name} '
                               f'that you can see.', empty=True,
                      citations=[_company_chip(company)],
                      sources=['opportunities.company_id'])
    total = sum(float(o.value_inr or 0) for o in opps)
    rows = [{'Opportunity': o.opp_number, 'Title': o.title or '—',
             'Value': _money(o.value_inr), 'Stage': o.stage,
             'Close': str(o.expected_close_date or '')[:10] or '—',
             'Owner': o.owner_emp_code or '—', '_chip': _opp_chip(o)}
            for o in opps]
    rows, notes = _cap(rows, len(opps))
    return Result(
        headline=(f'{company.name} — {len(opps)} open opportunity(s), '
                  f'{_money(total)}.'),
        columns=['Opportunity', 'Title', 'Value', 'Stage', 'Close', 'Owner'],
        rows=rows, figures={'count': len(opps), 'total': total},
        notes=notes, citations=[_company_chip(company)],
        filters={'account': company.name},
        sources=['opportunities.company_id', 'opportunities.value_inr'])


def _services_of(text):
    from app.copilot import vocabulary

    found = set()
    raw = str(text or '')
    for service, aliases in vocabulary.SERVICE_ALIASES.items():
        if any(a.lower() == raw.strip().lower() for a in aliases):
            found.add(service)
    hit = vocabulary.service_in_text(raw)
    if hit:
        found.add(hit)
    return found


@intent('cross_sell_account', 'Cross-sell for an account',
        params={'account': 'the customer name', 'account_id': 'or its id'},
        personas=('sales', 'head', 'mgmt'), phase=3,
        examples=('cross sell for Tata Steel', 'what else can we sell to JSW',
                  'which services does Godrej not buy'))
def cross_sell_account(scope, params):
    """Services an account buys against the services Procam sells.

    "Buys" is evidence of won business the viewer can see: a won
    opportunity's lead or account service, a handover's service list,
    or a line on a won quote. "Enquired" is a lead for that service that
    has not been won. Everything else Procam sells is the gap.
    """
    from app import Lead, Opportunity
    from app.copilot import vocabulary
    from app.models.quote import Quote, QuoteLine
    from app.models.tms_handover import WonHandover

    company, early = _account_subject(scope, params,
                                      lambda n: f'cross sell for {n}')
    if early is not None:
        return early
    if rec_mod.company_access(company, sc=scope) == rec_mod.ROUTING:
        return _routing_only(company)

    bought, enquired = {}, {}

    def note(bucket, services, evidence):
        for s in services:
            bucket.setdefault(s, evidence)

    won = (sc_mod.opportunities(sc=scope)
           .filter(Opportunity.company_id == company.id,
                   Opportunity.stage.in_(WON)).all())
    lead_ids = [o.lead_id for o in won if o.lead_id]
    lead_vert = {}
    if lead_ids:
        lead_vert = dict(sc_mod.leads(sc=scope)
                         .filter(Lead.id.in_(lead_ids))
                         .with_entities(Lead.id, Lead.procam_vertical).all())
    for o in won:
        found = _services_of(lead_vert.get(o.lead_id)) or \
            _services_of(company.vertical)
        note(bought, found, f'won {o.opp_number}')
    if scope.can('module.handovers'):
        for h in (rec_mod.handovers(sc=scope)
                  .filter(WonHandover.account_id == company.id).all()):
            for item in (h.services or []):
                label = item.get('name') if isinstance(item, dict) else item
                note(bought, _services_of(label), 'handover')
    if scope.can('module.quotes'):
        won_quotes = (rec_mod.quotes(sc=scope)
                      .filter(Quote.account_id == company.id,
                              Quote.status == 'Won')
                      .with_entities(Quote.id, Quote.quote_number).all())
        numbers = dict(won_quotes)
        if numbers:
            for qid, service in (QuoteLine.query
                                 .filter(QuoteLine.quote_id.in_(list(numbers)))
                                 .with_entities(QuoteLine.quote_id,
                                                QuoteLine.service).all()):
                note(bought, _services_of(service),
                     f'won quote {numbers.get(qid)}')
    for lid, stage, vert in (sc_mod.leads(sc=scope)
                             .filter(Lead.company_id == company.id)
                             .with_entities(Lead.id, Lead.stage,
                                            Lead.procam_vertical).all()):
        if stage not in WON:
            note(enquired, _services_of(vert), 'lead, not won')

    rows = []
    for service in vocabulary.crm_services():
        if service in bought:
            status, why = 'Buys', bought[service]
        elif service in enquired:
            status, why = 'Enquired, not won', enquired[service]
        else:
            status, why = 'Never', '—'
        rows.append({'Service': service, 'Status': status, 'Evidence': why,
                     '_chip': _company_chip(company)})
    gaps = [r['Service'] for r in rows if r['Status'] != 'Buys']
    buys = [r['Service'] for r in rows if r['Status'] == 'Buys']
    notes = ['"Buys" means won business you can see — a won opportunity, '
             'a handover or a won quote line. Services are the CRM service '
             'master\'s list; Sea and Air Freight are not in it.']
    if not buys:
        notes.append('No won business with a service recorded, so every '
                     'service shows as a gap. Treat this as a list of what '
                     'Procam sells, not evidence of what they lack.')
    return Result(
        headline=((f'{company.name} buys {", ".join(buys)}; '
                   if buys else f'{company.name} has no won service on '
                                f'record; ')
                  + (f'not {", ".join(gaps)}.' if gaps else
                     'every Procam service is covered.')),
        columns=['Service', 'Status', 'Evidence'], rows=rows,
        figures={'buys': len(buys), 'gaps': len(gaps)}, notes=notes,
        recommendation=bool(gaps), citations=[_company_chip(company)],
        filters={'account': company.name},
        sources=['opportunities.stage', 'leads.procam_vertical',
                 'won_handovers.services', 'quote_lines.service'])


@intent('relationship_map', 'Who knows this account',
        params={'account': 'the customer name', 'account_id': 'or its id'},
        personas=('sales', 'head', 'mgmt', 'ops'), phase=3,
        examples=('who knows Tata Steel', 'who at Procam has worked with JSW',
                  'relationship map for Godrej'))
def relationship_map(scope, params):
    """Who at Procam has touched an account, and when they last did.

    Built from roles (owner, secondary, lead and opportunity owners) and
    from touches on the leads the viewer can see (activities logged,
    emails sent). The owner is routing information and always shown; the
    rest needs the viewer to see into the account.
    """
    from app import LeadActivity, LeadEmail, Lead, Opportunity

    company, early = _account_subject(scope, params,
                                      lambda n: f'who knows {n}')
    if early is not None:
        return early
    if rec_mod.company_access(company, sc=scope) == rec_mod.ROUTING:
        return _routing_only(company)

    people = {}

    def add(code, role, when=None, touch=False):
        if not code:
            return
        p = people.setdefault(code, {'roles': [], 'touches': 0,
                                     'last': None})
        if role and role not in p['roles']:
            p['roles'].append(role)
        if touch:
            p['touches'] += 1
        if when and (p['last'] is None or when > p['last']):
            p['last'] = when

    add(company.pic_emp_code, 'account owner')
    add(company.secondary_pic_emp_code, 'secondary PIC')
    leads = (sc_mod.leads(sc=scope).filter(Lead.company_id == company.id)
             .with_entities(Lead.id, Lead.assigned_to, Lead.secondary_owner)
             .all())
    lead_ids = [l.id for l in leads]
    for l in leads:
        add(l.assigned_to, 'lead owner')
        add(l.secondary_owner, 'lead secondary')
    for o in (sc_mod.opportunities(sc=scope)
              .filter(Opportunity.company_id == company.id)
              .with_entities(Opportunity.owner_emp_code).all()):
        add(o.owner_emp_code, 'opportunity owner')
    if lead_ids:
        for code, when in (sc_mod.activities(sc=scope)
                           .filter(LeadActivity.lead_id.in_(lead_ids[:500]))
                           .with_entities(LeadActivity.performed_by,
                                          LeadActivity.occurred_at).all()):
            add(code, 'logged activity', when, touch=True)
        for code, when in (sc_mod.emails(sc=scope)
                           .filter(LeadEmail.lead_id.in_(lead_ids[:500]),
                                   LeadEmail.direction == 'outbound')
                           .with_entities(LeadEmail.created_by,
                                          LeadEmail.sent_or_received_at)
                           .all()):
            add(code, 'emailed the customer', when, touch=True)
    contacts = (sc_mod.contacts(sc=scope)
                .filter(_contact_company_col() == company.id).count())
    names = _names(people)
    rows = [{'Person': names.get(code, code), 'Role': ', '.join(p['roles']),
             'Touches': p['touches'],
             'Last touch': str(p['last'] or '')[:10] or '—'}
            for code, p in sorted(people.items(),
                                  key=lambda kv: (-(kv[1]['touches']),
                                                  kv[0]))]
    if not rows:
        return Result(headline=f'Nobody at Procam is recorded against '
                               f'{company.name}.', empty=True,
                      citations=[_company_chip(company)],
                      sources=['Account Master', 'leads'])
    last = max((p['last'] for p in people.values() if p['last']),
               default=None)
    return Result(
        headline=(f'{company.name} — {len(rows)} person(s) at Procam; '
                  f'{contacts} contact(s) on record'
                  + (f'; last touch {str(last)[:10]}.' if last else
                     '; no touch logged.')),
        columns=['Person', 'Role', 'Touches', 'Last touch'], rows=rows,
        figures={'people': len(rows), 'contacts': contacts},
        notes=['Touches are activities logged and emails sent on the leads '
               'you can see. A call nobody logged is not here.'],
        citations=[_company_chip(company)],
        filters={'account': company.name},
        sources=['Account Master', 'leads.assigned_to',
                 'lead_activities.performed_by', 'lead_emails.created_by'])


_ROLE_ORDER = {'Decision Maker': 0, 'Influencer': 1, 'User': 2}


@intent('key_contacts', 'Key contacts',
        params={'account': 'the customer name', 'account_id': 'or its id',
                'lead_id': 'or the lead in context'},
        personas=('sales', 'head', 'ops'), phase=3,
        examples=('who is the contact at Tata Steel', 'key contacts for JSW',
                  'decision makers at Godrej'))
def key_contacts(scope, params):
    """The customer's people: the lead's contact when a lead is in hand,
    otherwise the account's contacts the viewer may see."""
    from app import Lead, Opportunity

    ctx = params.get('context') or {}
    if not params.get('account') and (
            params.get('lead_id') or ctx.get('type') == 'lead'):
        lead = _resolve_lead(scope, params)
        if lead is None:
            return Result(headline='That lead is outside what you can see, '
                                   'or does not exist.',
                          restricted=True, empty=True, sources=['leads'])
        if not (lead.pic or lead.email or lead.phone):
            if lead.company_id:
                params = dict(params, account_id=lead.company_id,
                              context=None)
            else:
                return Result(headline=f'No contact person is recorded on '
                                       f'{lead.company}.', empty=True,
                              citations=[_lead_chip(lead)],
                              sources=[f'Lead #{lead.id}'])
        else:
            return Result(
                headline=(f'{lead.company} — {lead.pic or "unnamed contact"}'
                          + (f', {lead.designation_pic}'
                             if lead.designation_pic else '') + '.'),
                columns=['Name', 'Designation', 'Email', 'Phone'],
                rows=[{'Name': lead.pic or '—',
                       'Designation': lead.designation_pic or '—',
                       'Email': lead.email or '—',
                       'Phone': lead.phone or '—',
                       '_chip': _lead_chip(lead)}],
                citations=[_lead_chip(lead)],
                sources=[f'Lead #{lead.id}'])
    if not params.get('account') and ctx.get('type') in ('opportunity',
                                                          'opp'):
        opp = (sc_mod.opportunities(sc=scope)
               .filter(Opportunity.id == _int(ctx.get('id'), 0)).first())
        if opp is None:
            return Result(headline='That opportunity is outside what you can '
                                   'see.', restricted=True, empty=True,
                          sources=['opportunities'])
        params = dict(params, account_id=opp.company_id, context=None)

    company, early = _account_subject(scope, params,
                                      lambda n: f'key contacts for {n}')
    if early is not None:
        return early
    if rec_mod.company_access(company, sc=scope) == rec_mod.ROUTING:
        return _routing_only(company)
    people = (sc_mod.contacts(sc=scope)
              .filter(_contact_company_col() == company.id).all())
    if not people:
        pics = (sc_mod.leads(sc=scope)
                .filter(Lead.company_id == company.id, Lead.pic.isnot(None),
                        Lead.pic != '')
                .order_by(Lead.updated_at.desc().nullslast())
                .with_entities(Lead.id, Lead.company, Lead.pic,
                               Lead.designation_pic, Lead.email).limit(5)
                .all())
        if not pics:
            return Result(headline=f'No contacts recorded for {company.name}.',
                          empty=True, citations=[_company_chip(company)],
                          sources=['contacts', 'leads.pic'])
        return Result(
            headline=(f'{company.name} has no contact records; the leads name '
                      f'{len(pics)} person(s).'),
            columns=['Name', 'Designation', 'Email'],
            rows=[{'Name': l.pic, 'Designation': l.designation_pic or '—',
                   'Email': l.email or '—', '_chip': _lead_chip(l)}
                  for l in pics],
            citations=[_company_chip(company)], sources=['leads.pic'])
    people.sort(key=lambda c: (_ROLE_ORDER.get(c.decision_role or '', 9),
                               (c.name or '').lower()))
    rows = [{'Name': c.name, 'Designation': c.designation or '—',
             'Role': c.decision_role or '—',
             'Strength': c.relationship_strength or '—',
             'Email': c.email or '—', 'Phone': c.mobile or c.phone or '—',
             '_chip': _company_chip(company)} for c in people]
    rows, notes = _cap(rows, len(people))
    makers = sum(1 for c in people if c.decision_role == 'Decision Maker')
    return Result(
        headline=(f'{company.name} — {len(people)} contact(s), {makers} '
                  f'decision maker(s).'),
        columns=['Name', 'Designation', 'Role', 'Strength', 'Email', 'Phone'],
        rows=rows, figures={'count': len(people), 'decision_makers': makers},
        notes=notes, citations=[_company_chip(company)],
        filters={'account': company.name}, sources=['contacts'])


@intent('top_accounts', 'Top accounts',
        permission='reports.accounts',
        params={'days': 'window for won business in days (default 365)'},
        personas=('head', 'mgmt'), phase=2,
        examples=('top accounts', 'our biggest customers',
                  'accounts by revenue'))
def top_accounts(scope, params):
    from app import Company, Opportunity

    days = _int(params.get('days'), 365)
    since = _days_ago(days)
    won, open_ = {}, {}
    for cid, stage, value, won_at in (
            sc_mod.opportunities(sc=scope)
            .filter(Opportunity.company_id.isnot(None))
            .with_entities(Opportunity.company_id, Opportunity.stage,
                           Opportunity.value_inr, Opportunity.won_at).all()):
        if stage in WON and (won_at is None or won_at >= since):
            won[cid] = won.get(cid, 0.0) + float(value or 0)
        elif stage not in _OPP_CLOSED:
            open_[cid] = open_.get(cid, 0.0) + float(value or 0)
    ids = set(won) | set(open_)
    if not ids:
        return Result(headline='No won or open business in your scope.',
                      empty=True, sources=['opportunities'])
    ranked = sorted(ids, key=lambda i: (-won.get(i, 0.0),
                                        -open_.get(i, 0.0), i))[:ROW_CAP]
    names = {c.id: c for c in Company.query.filter(Company.id.in_(ranked))}
    rows = [{'Account': names[i].name if i in names else f'Account {i}',
             f'Won ({days}d)': _money(won.get(i, 0.0)),
             'Open pipeline': _money(open_.get(i, 0.0)),
             '_chip': {'type': 'company', 'id': i,
                       'label': names[i].name if i in names else str(i)}}
            for i in ranked]
    notes = ([f'Showing the first {ROW_CAP} of {len(ids)}.']
             if len(ids) > ROW_CAP else [])
    notes.append('Ranked by won value in the window, then open pipeline; '
                 'only opportunities you can see are counted.')
    return Result(
        headline=(f'{len(ids)} account(s) with business; top by won value: '
                  f'{rows[0]["Account"]}.'),
        columns=['Account', f'Won ({days}d)', 'Open pipeline'], rows=rows,
        figures={'count': len(ids), 'won_total': sum(won.values()),
                 'open_total': sum(open_.values())},
        notes=notes, sources=['opportunities.value_inr',
                              'opportunities.won_at'])


# ══════════════════════════════════════════════════════════════════════
#  LEADS, WINS AND LOSSES
# ══════════════════════════════════════════════════════════════════════
@intent('leads_new', 'New leads',
        params={'days': 'window in days (default 7)', 'vertical': VERT},
        personas=('sales', 'head', 'mgmt'), phase=2,
        examples=('new leads this week', 'leads that came in today',
                  'how many leads were created this month'))
def leads_new(scope, params):
    from app import Lead

    days = _int(params.get('days'), 7)
    service = _service_param(params)
    base = sc_mod.leads(sc=scope).filter(Lead.created_at >= _days_ago(days))
    if service:
        base = _narrow_leads_to_service(base, service)
    total = base.count()
    if not total:
        return Result(headline=f'No new leads in the last {days} days.',
                      empty=True, filters=_service_filters(service),
                      sources=['leads.created_at'])
    found = (base.with_entities(Lead.id, Lead.company, Lead.source,
                                Lead.stage, Lead.assigned_to,
                                Lead.created_at)
             .order_by(Lead.created_at.desc(), Lead.id.desc())
             .limit(ROW_CAP).all())
    rows = [{'Company': l.company, 'Source': l.source or '—',
             'Stage': l.stage, 'Owner': l.assigned_to or 'unassigned',
             'Created': str(l.created_at or '')[:10],
             '_chip': _lead_chip(l)} for l in found]
    rows, notes = _cap(rows, total)
    return Result(
        headline=f'{total} new lead(s) in the last {days} days.',
        columns=['Company', 'Source', 'Stage', 'Owner', 'Created'],
        rows=rows, figures={'count': total, 'days': days}, notes=notes,
        filters=_service_filters(service), sources=['leads.created_at'])


def _stage_order():
    try:
        from app import STAGES_ALL
        return {s: i for i, s in enumerate(STAGES_ALL)}
    except Exception:                                  # pragma: no cover
        return {}


@intent('leads_by_stage', 'Leads by stage',
        params={'vertical': VERT},
        personas=('sales', 'head', 'mgmt'), phase=2,
        examples=('leads by stage', 'lead funnel',
                  'how many leads are at each stage'))
def leads_by_stage(scope, params):
    from app import Lead

    service = _service_param(params)
    base = sc_mod.leads(sc=scope)
    if service:
        base = _narrow_leads_to_service(base, service)
    grouped = (base.with_entities(Lead.stage, func.count(Lead.id))
               .group_by(Lead.stage).all())
    if not grouped:
        return Result(headline='No leads in your scope.', empty=True,
                      filters=_service_filters(service),
                      sources=['leads.stage'])
    values = {}
    for stage, value in base.with_entities(Lead.stage,
                                           Lead.estimated_value_inr).all():
        values[stage] = values.get(stage, 0.0) + float(value or 0)
    order = _stage_order()
    rows = [{'Stage': s or '—', 'Leads': n, 'Value': _money(values.get(s))}
            for s, n in sorted(grouped, key=lambda r: (order.get(r[0], 99),
                                                       r[0] or ''))]
    total = sum(n for _s, n in grouped)
    open_n = sum(n for s, n in grouped if (s or '') not in _TERMINAL)
    return Result(
        headline=f'{total} lead(s) — {open_n} open, {total - open_n} closed.',
        columns=['Stage', 'Leads', 'Value'], rows=rows,
        figures={'count': total, 'open': open_n},
        filters=_service_filters(service), sources=['leads.stage'])


@intent('lead_sources', 'Where leads come from',
        params={'days': 'window in days (default 90)'},
        personas=('head', 'mgmt'), phase=2,
        examples=('lead sources', 'where do our leads come from',
                  'leads by source'))
def lead_sources(scope, params):
    from app import Lead

    days = _int(params.get('days'), 90)
    base = sc_mod.leads(sc=scope).filter(Lead.created_at >= _days_ago(days))
    grouped = (base.with_entities(Lead.source, func.count(Lead.id))
               .group_by(Lead.source).all())
    if not grouped:
        return Result(headline=f'No leads created in the last {days} days.',
                      empty=True, sources=['leads.source'])
    won = dict(base.filter(Lead.stage == 'Won')
               .with_entities(Lead.source, func.count(Lead.id))
               .group_by(Lead.source).all())
    total = sum(n for _s, n in grouped)
    rows = [{'Source': s or 'unknown', 'Leads': n, 'Won': won.get(s, 0),
             'Share': f'{round(100 * n / total)}%'}
            for s, n in sorted(grouped, key=lambda r: (-r[1], r[0] or ''))]
    return Result(
        headline=(f'{total} lead(s) in the last {days} days from '
                  f'{len(rows)} source(s); most from {rows[0]["Source"]}.'),
        columns=['Source', 'Leads', 'Won', 'Share'], rows=rows,
        figures={'count': total, 'days': days}, sources=['leads.source'])


@intent('leads_unassigned', 'Unassigned leads',
        personas=('admin', 'head', 'mgmt'), phase=2,
        examples=('unassigned leads', 'leads with no owner',
                  'which leads is nobody working'))
def leads_unassigned(scope, params):
    """Only a company-wide scope can see a lead nobody owns — every other
    scope is defined by owners. Said plainly, without a count, rather
    than answering "none" to someone who could never see one."""
    from app import Lead

    if not scope.unrestricted:
        return Result(
            headline='Unassigned leads are visible only to people whose CRM '
                     'access covers the whole company.',
            restricted=True, empty=True, sources=['leads.assigned_to'])
    base = (sc_mod.leads(sc=scope)
            .filter(~Lead.stage.in_(_TERMINAL))
            .filter(or_(Lead.assigned_to.is_(None), Lead.assigned_to == '')))
    total = base.count()
    if not total:
        return Result(headline='Every open lead has an owner.', empty=True,
                      sources=['leads.assigned_to'])
    found = (base.with_entities(Lead.id, Lead.company, Lead.stage,
                                Lead.source, Lead.created_at)
             .order_by(Lead.created_at.desc(), Lead.id.desc())
             .limit(ROW_CAP).all())
    rows = [{'Company': l.company, 'Stage': l.stage,
             'Source': l.source or '—',
             'Created': str(l.created_at or '')[:10],
             '_chip': _lead_chip(l)} for l in found]
    rows, notes = _cap(rows, total)
    return Result(headline=f'{total} open lead(s) with no owner.',
                  columns=['Company', 'Stage', 'Source', 'Created'],
                  rows=rows, figures={'count': total}, notes=notes,
                  sources=['leads.assigned_to'])


@intent('deals_won_recent', 'Recent wins',
        params={'days': 'window in days (default 30)', 'vertical': VERT},
        personas=('sales', 'head', 'mgmt'), phase=2,
        examples=('what did we win this month', 'recent wins',
                  'deals won in the last 30 days'))
def deals_won_recent(scope, params):
    from app import Company, Opportunity

    days = _int(params.get('days'), 30)
    service = _service_param(params)
    base = sc_mod.opportunities(sc=scope).filter(
        Opportunity.stage.in_(WON), Opportunity.won_at >= _days_ago(days))
    if service:
        base = _narrow_opps_to_service(base, service)
    opps = base.order_by(Opportunity.won_at.desc(), Opportunity.id.desc()).all()
    if not opps:
        return Result(headline=f'Nothing won in the last {days} days.',
                      empty=True, filters=_service_filters(service),
                      sources=['opportunities.won_at'])
    total = sum(float(o.value_inr or 0) for o in opps)
    names = {c.id: c.name for c in Company.query.filter(
        Company.id.in_([o.company_id for o in opps if o.company_id]))}
    rows = [{'Opportunity': o.opp_number,
             'Account': names.get(o.company_id, '—'),
             'Value': _money(o.value_inr), 'Won': str(o.won_at or '')[:10],
             'Owner': o.owner_emp_code or '—', '_chip': _opp_chip(o)}
            for o in opps]
    rows, notes = _cap(rows, len(opps))
    return Result(
        headline=(f'{len(opps)} deal(s) won in the last {days} days, '
                  f'{_money(total)}.'),
        columns=['Opportunity', 'Account', 'Value', 'Won', 'Owner'],
        rows=rows, figures={'count': len(opps), 'value': total,
                            'days': days},
        notes=notes, filters=_service_filters(service),
        sources=['opportunities.won_at', 'opportunities.value_inr'])


@intent('deals_lost_recent', 'Recent losses',
        params={'days': 'window in days (default 30)', 'vertical': VERT},
        personas=('sales', 'head', 'mgmt'), phase=2,
        examples=('what did we lose this month', 'recent losses',
                  'leads lost in the last 30 days'))
def deals_lost_recent(scope, params):
    """Lost leads in the window. The reason column needs the same
    permission loss analysis does — it is the competitive half."""
    from app import Lead

    days = _int(params.get('days'), 30)
    service = _service_param(params)
    base = sc_mod.leads(sc=scope).filter(Lead.stage == 'Lost',
                                         Lead.updated_at >= _days_ago(days))
    if service:
        base = _narrow_leads_to_service(base, service)
    total = base.count()
    if not total:
        return Result(headline=f'No leads lost in the last {days} days.',
                      empty=True, filters=_service_filters(service),
                      sources=['leads.stage'])
    with_reason = scope.can('reports.competitor')
    found = (base.with_entities(Lead.id, Lead.company, Lead.lost_reason,
                                Lead.estimated_value_inr, Lead.assigned_to,
                                Lead.updated_at)
             .order_by(Lead.updated_at.desc(), Lead.id.desc())
             .limit(ROW_CAP).all())
    rows = []
    for l in found:
        row = {'Company': l.company, 'Value': _money(l.estimated_value_inr),
               'Lost': str(l.updated_at or '')[:10],
               'Owner': l.assigned_to or '—', '_chip': _lead_chip(l)}
        if with_reason:
            row['Reason'] = l.lost_reason or 'not recorded'
        rows.append(row)
    rows, notes = _cap(rows, total)
    notes.append('Dated by the lead\'s last update, which is when it was '
                 'marked lost unless it was edited since.')
    columns = ['Company', 'Value', 'Lost', 'Owner'] + (
        ['Reason'] if with_reason else [])
    return Result(headline=f'{total} lead(s) lost in the last {days} days.',
                  columns=columns, rows=rows,
                  figures={'count': total, 'days': days}, notes=notes,
                  filters=_service_filters(service),
                  sources=['leads.stage', 'leads.updated_at'])


# ══════════════════════════════════════════════════════════════════════
#  PERFORMANCE
# ══════════════════════════════════════════════════════════════════════
#: Per-vertical win rates below this many decided deals are shown as
#: counts only. The overall figure keeps MIN_SAMPLE_FOR_ANALYSIS.
MIN_SAMPLE_PER_BUCKET = 5
#: One person's own win rate needs fewer decided deals than a team's.
MIN_SAMPLE_PERSONAL = 10


@intent('win_rate_by_vertical', 'Win rate by vertical',
        permission='reports.accounts',
        params={'days': 'window in days (default 365)'},
        personas=('head', 'mgmt'), phase=2,
        examples=('win rate by vertical', 'conversion rate by service',
                  'vertical wise win rate'))
def win_rate_by_vertical(scope, params):
    from app import Company, Lead, Opportunity
    from app.copilot import vocabulary

    days = _int(params.get('days'), 365)
    decided = (sc_mod.opportunities(sc=scope)
               .filter(Opportunity.created_at >= _days_ago(days),
                       Opportunity.stage.in_(WON + LOST))
               .with_entities(Opportunity.stage, Opportunity.value_inr,
                              Opportunity.company_id, Opportunity.lead_id)
               .all())
    if not decided:
        return Result(headline=f'No opportunity was won or lost in the last '
                               f'{days} days.', empty=True,
                      sources=['opportunities.stage'])
    comp_vert = dict(Company.query.filter(Company.id.in_(
        {o.company_id for o in decided if o.company_id}))
        .with_entities(Company.id, Company.vertical).all())
    lead_vert = dict(Lead.query.filter(Lead.id.in_(
        {o.lead_id for o in decided if o.lead_id}))
        .with_entities(Lead.id, Lead.procam_vertical).all())
    buckets = {}
    for o in decided:
        raw = comp_vert.get(o.company_id) or lead_vert.get(o.lead_id) or ''
        service = (vocabulary.service_in_text(raw) or raw or 'unspecified')
        b = buckets.setdefault(service, {'won': 0, 'lost': 0, 'value': 0.0})
        if o.stage in WON:
            b['won'] += 1
            b['value'] += float(o.value_inr or 0)
        else:
            b['lost'] += 1
    rows = []
    for service, b in sorted(buckets.items(),
                             key=lambda kv: -(kv[1]['won'] + kv[1]['lost'])):
        n = b['won'] + b['lost']
        rows.append({'Vertical': service, 'Won': b['won'], 'Lost': b['lost'],
                     'Win rate': (f'{round(100 * b["won"] / n)}%'
                                  if n >= MIN_SAMPLE_PER_BUCKET
                                  else 'too few to say'),
                     'Won value': _money(b['value'])})
    won = sum(b['won'] for b in buckets.values())
    n = len(decided)
    overall = (f'{round(100 * won / n)}% overall' if n >=
               MIN_SAMPLE_FOR_ANALYSIS else f'{n} decided — too few for an '
                                             f'overall rate')
    return Result(
        headline=f'Win rate by vertical, last {days} days — {overall}.',
        columns=['Vertical', 'Won', 'Lost', 'Win rate', 'Won value'],
        rows=rows, figures={'decided': n, 'won': won, 'window_days': days},
        notes=[f'A vertical\'s rate is shown only with '
               f'{MIN_SAMPLE_PER_BUCKET}+ decided deals, the overall rate '
               f'with {MIN_SAMPLE_FOR_ANALYSIS}+. The vertical is the '
               f'account\'s, else the lead\'s; only opportunities you can '
               f'see are counted.'],
        sources=['opportunities.stage', 'companies.vertical',
                 'leads.procam_vertical'])


@intent('my_win_rate', 'My win rate',
        params={'days': 'window in days (default 365)'},
        personas=('sales', 'head'), phase=2,
        examples=('my win rate', 'my own conversion rate',
                  'how many deals have I won'))
def my_win_rate(scope, params):
    """The asker's own records only — owner is the asker, inside the
    scope. "My" never widens to a vertical head's team here; that is
    conversion_rate, and it carries its own permission."""
    from app import Lead, Opportunity

    me = scope.emp_code
    if not me:
        return Result(headline='Sign in to see your own numbers.', empty=True)
    days = _int(params.get('days'), 365)
    since = _days_ago(days)
    decided = (sc_mod.opportunities(sc=scope)
               .filter(Opportunity.owner_emp_code == me,
                       Opportunity.created_at >= since,
                       Opportunity.stage.in_(WON + LOST))
               .with_entities(Opportunity.stage, Opportunity.value_inr).all())
    won = [o for o in decided if o.stage in WON]
    leads_in = (sc_mod.leads(sc=scope)
                .filter(Lead.assigned_to == me, Lead.created_at >= since))
    lead_total = leads_in.count()
    lead_won = leads_in.filter(Lead.stage == 'Won').count()
    figures = {'decided': len(decided), 'won': len(won),
               'won_value': sum(float(o.value_inr or 0) for o in won),
               'leads': lead_total, 'leads_won': lead_won,
               'window_days': days}
    rows = [{'Measure': 'Opportunities won / decided',
             'Value': f'{len(won)} / {len(decided)}'},
            {'Measure': 'Leads won / created',
             'Value': f'{lead_won} / {lead_total}'},
            {'Measure': 'Won value', 'Value': _money(figures['won_value'])}]
    if len(decided) < MIN_SAMPLE_PERSONAL:
        return Result(
            headline=(f'{len(won)} won of {len(decided)} decided in the last '
                      f'{days} days — too few to quote a rate.'),
            columns=['Measure', 'Value'], rows=rows, figures=figures,
            notes=[f'A personal rate is given from {MIN_SAMPLE_PERSONAL} '
                   f'decided opportunities.'],
            sources=['opportunities.owner_emp_code', 'leads.assigned_to'])
    pct = round(100 * len(won) / len(decided))
    figures['rate_pct'] = pct
    return Result(
        headline=(f'Your win rate is {pct}% — {len(won)} of {len(decided)} '
                  f'decided opportunities in the last {days} days.'),
        columns=['Measure', 'Value'], rows=rows, figures=figures,
        notes=['Only opportunities and leads you own are counted.'],
        sources=['opportunities.owner_emp_code', 'leads.assigned_to'])


@intent('team_workload', 'Team workload',
        permission='reports.action', personas=('head', 'mgmt'), phase=2,
        examples=('team workload', 'who has the most open leads',
                  'leads per salesperson'))
def team_workload(scope, params):
    from app import Employee, Lead, Opportunity

    people = (sc_mod.employees(sc=scope)
              .filter(Employee.is_active.is_(True))
              .with_entities(Employee.emp_code, Employee.name).all())
    if not people:
        return Result(headline='Nobody in your scope.', empty=True,
                      sources=['employees'])
    codes = [p.emp_code for p in people]
    today = _now().date()
    open_leads = (sc_mod.leads(sc=scope)
                  .filter(~Lead.stage.in_(_TERMINAL),
                          Lead.assigned_to.in_(codes)))
    leads_n = dict(open_leads.with_entities(Lead.assigned_to,
                                            func.count(Lead.id))
                   .group_by(Lead.assigned_to).all())
    overdue = dict(open_leads.filter(Lead.followup_date < today)
                   .with_entities(Lead.assigned_to, func.count(Lead.id))
                   .group_by(Lead.assigned_to).all())
    opps_n, opps_v = {}, {}
    for code, value in (sc_mod.opportunities(sc=scope)
                        .filter(~Opportunity.stage.in_(_OPP_CLOSED),
                                Opportunity.owner_emp_code.in_(codes))
                        .with_entities(Opportunity.owner_emp_code,
                                       Opportunity.value_inr).all()):
        opps_n[code] = opps_n.get(code, 0) + 1
        opps_v[code] = opps_v.get(code, 0.0) + float(value or 0)
    rows = [{'Person': p.name or p.emp_code, 'Code': p.emp_code,
             'Open leads': leads_n.get(p.emp_code, 0),
             'Overdue follow-ups': overdue.get(p.emp_code, 0),
             'Open opportunities': opps_n.get(p.emp_code, 0),
             'Open value': _money(opps_v.get(p.emp_code, 0.0))}
            for p in people]
    rows.sort(key=lambda r: (-r['Open leads'], -r['Overdue follow-ups'],
                             r['Code']))
    rows, notes = _cap(rows, len(rows))
    busiest = rows[0]
    return Result(
        headline=(f'{len(people)} people; most open leads: '
                  f'{busiest["Person"]} ({busiest["Open leads"]}).'),
        columns=['Person', 'Code', 'Open leads', 'Overdue follow-ups',
                 'Open opportunities', 'Open value'],
        rows=rows, figures={'people': len(people),
                            'open_leads': sum(leads_n.values())},
        notes=notes, sources=['leads.assigned_to', 'leads.followup_date',
                              'opportunities.owner_emp_code'])


# ══════════════════════════════════════════════════════════════════════
#  DATA QUALITY AND ADMINISTRATION
# ══════════════════════════════════════════════════════════════════════
@intent('dq_accounts_no_owner', 'Accounts with no owner',
        personas=('admin', 'head', 'sales'), phase=2,
        examples=('accounts with no owner', 'unowned accounts',
                  'which customers have no PIC'))
def dq_accounts_no_owner(scope, params):
    """Ownerless accounts the viewer is connected to.

    A restricted scope is defined by owners, so it can never contain an
    ownerless account directly; what it can contain is leads and
    opportunities on one. Those accounts are the ones this viewer should
    ask to have assigned. A company-wide scope sees them all.
    """
    from app import Company, Lead, Opportunity

    no_owner = or_(Company.pic_emp_code.is_(None), Company.pic_emp_code == '')
    if scope.unrestricted:
        base = (sc_mod.companies(sc=scope)
                .filter(Company.is_active.is_(True), no_owner))
    else:
        linked = {r[0] for r in sc_mod.leads(sc=scope)
                  .with_entities(Lead.company_id).distinct() if r[0]}
        linked |= {r[0] for r in sc_mod.opportunities(sc=scope)
                   .with_entities(Opportunity.company_id).distinct() if r[0]}
        if not linked:
            return Result(headline='None of your leads or opportunities is '
                                   'on an account without an owner.',
                          empty=True, sources=['companies.pic_emp_code'])
        base = Company.query.filter(Company.id.in_(list(linked)[:5000]),
                                    Company.is_active.is_(True), no_owner)
    total = base.count()
    if not total:
        return Result(headline=('Every active account has an owner.'
                                if scope.unrestricted else
                                'None of your leads or opportunities is on '
                                'an account without an owner.'),
                      empty=True, sources=['companies.pic_emp_code'])
    found = base.order_by(Company.name.asc(), Company.id.asc()) \
        .limit(ROW_CAP).all()
    counts = dict(sc_mod.leads(sc=scope)
                  .filter(Lead.company_id.in_([c.id for c in found]),
                          ~Lead.stage.in_(_TERMINAL))
                  .with_entities(Lead.company_id, func.count(Lead.id))
                  .group_by(Lead.company_id).all())
    rows = [{'Account': c.name, 'Vertical': c.vertical or '—',
             'Your open leads' if not scope.unrestricted else 'Open leads':
                 counts.get(c.id, 0),
             '_chip': _company_chip(c)} for c in found]
    rows, notes = _cap(rows, total)
    notes.append('An account with no owner cannot auto-assign its leads. '
                 'An administrator assigns owners on the Account Master.')
    lead_col = 'Your open leads' if not scope.unrestricted else 'Open leads'
    return Result(headline=f'{total} account(s) with no owner.',
                  columns=['Account', 'Vertical', lead_col], rows=rows,
                  figures={'count': total}, notes=notes,
                  sources=['companies.pic_emp_code'])


@intent('intake_review_pending', 'Emails awaiting intake review',
        permission='admin.master', personas=('admin',), phase=2,
        examples=('intake review queue', 'emails pending review',
                  'unreviewed enquiries'))
def intake_review_pending(scope, params):
    """Counts only — never a subject or a sender. The queue holds mail
    that has not been judged yet, including mail that is not a lead and
    was never meant to reach anyone's CRM view."""
    from app import EmailClassification

    if not scope.unrestricted:
        return Result(
            headline='The intake review queue is visible only to people '
                     'whose CRM access covers the whole company.',
            restricted=True, empty=True,
            sources=['email_classifications.review_state'])
    grouped = (EmailClassification.query
               .filter(EmailClassification.review_state == 'pending')
               .with_entities(EmailClassification.classification,
                              func.count(EmailClassification.id),
                              func.min(EmailClassification.created_at))
               .group_by(EmailClassification.classification).all())
    if not grouped:
        return Result(headline='Nothing is waiting in the intake review '
                               'queue.', empty=True,
                      sources=['email_classifications.review_state'])
    total = sum(n for _c, n, _t in grouped)
    rows = [{'Classified as': c or '—', 'Emails': n,
             'Oldest': str(t or '')[:10] or '—'}
            for c, n, t in sorted(grouped, key=lambda r: -r[1])]
    return Result(
        headline=f'{total} email(s) waiting for intake review.',
        columns=['Classified as', 'Emails', 'Oldest'], rows=rows,
        figures={'count': total},
        notes=['The Intake screen shows the emails themselves.'],
        sources=['email_classifications.review_state'])


@intent('my_tasks', 'My open tasks',
        personas=('sales', 'head', 'ops', 'admin'), phase=2,
        examples=('my open tasks', 'overdue tasks', 'my work queue'))
def my_tasks(scope, params):
    """The My Work queue, read through the task engine's own definition
    of whose task it is, so this list and the My Work page agree."""
    from app import Employee
    from app.services import task_engine

    emp = (Employee.query.filter_by(emp_code=scope.emp_code).first()
           if scope.emp_code else None)
    if emp is None:
        return Result(headline='Sign in to see your tasks.', empty=True)
    try:
        work = task_engine.my_work(emp)
    except Exception:
        return Result(headline='The task queue is not available on this '
                               'server yet.', empty=True,
                      sources=['task_instances'])
    buckets = (('overdue', 'overdue'), ('due_today', 'due today'),
               ('action_required', 'to do'))
    rows = []
    seen = set()
    for key, label in buckets:
        for t in work.get(key) or []:
            if t.id in seen:
                continue
            seen.add(t.id)
            chip = None
            if t.entity_type in ('lead', 'opportunity', 'quote', 'rfq') \
                    and t.entity_id:
                chip = {'type': t.entity_type, 'id': t.entity_id,
                        'label': t.entity_display or t.task_key}
            row = {'Task': (t.task_key or '').replace('_', ' '),
                   'Record': t.entity_display or f'{t.entity_type} '
                                                 f'{t.entity_id}',
                   'Due': str(t.due_at or '')[:10] or '—', 'Status': label}
            if chip:
                row['_chip'] = chip
            rows.append(row)
    if not rows:
        return Result(headline='No open tasks in your queue.', empty=True,
                      sources=['My Work'])
    counts = {k: len(work.get(k) or []) for k, _l in buckets}
    rows, notes = _cap(rows, len(rows))
    return Result(
        headline=(f'{counts["overdue"]} overdue · {counts["due_today"]} due '
                  f'today · {counts["action_required"]} to do.'),
        columns=['Task', 'Record', 'Due', 'Status'], rows=rows,
        figures=counts, notes=notes, sources=['My Work', 'task_instances'])
