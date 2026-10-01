"""
The CRM Hygiene Score — Group M.

One number for "is this CRM being kept properly?", and — far more
important — the arithmetic behind it in full. A score nobody can argue
with is a score nobody acts on, so every deduction here names the check
that caused it, how many records it flagged, out of how many it could
have flagged, and a link that opens exactly those records.

Nothing new is measured
    Every factor is computed from the Data Quality checks that already
    exist (``app/data_quality/definitions.py`` for what each check is and
    which thresholds it uses, ``app/data_quality/service.py`` for how it
    is measured against the live database). A second definition of
    "stale" or "unowned" living here would mean the Hygiene Score and the
    Data Quality dashboard could disagree about the same record, and a
    user who sees that stops believing both.

    What is new is only the arithmetic: which checks group into a factor,
    what each factor is worth, and what each is measured against.

How a factor scores
    ``lost = weight × (records flagged ÷ records that could be flagged)``

    The denominator is the population the factor applies to — open leads
    for the follow-up factors, won business for the handover factor, and
    so on — taken from the same "open", "live" and "won" definitions the
    checks themselves use. A factor with nothing in its population
    deducts nothing: there is no credit and no punishment for work that
    does not exist.

    The share is capped at 1, because two checks can flag the same
    record, and no factor may cost more than it is worth.

Scope
    Every count, every population and every record list is measured
    inside the viewer's Access Matrix boundary (``app/access/scope.py``).
    The three levels — one person, one vertical, the whole company — are
    the same calculation over a narrower scope, and narrowing can never
    widen: asking about somebody outside the viewer's boundary is refused
    rather than quietly answered.

    Records with no owner of their own are invisible to a restricted
    viewer, by the scope module's design. A rep's score therefore never
    punishes them for a lead nobody owns; the company-wide score does.
"""
from __future__ import annotations

from datetime import datetime

from app.data_quality import definitions as defs
from app.data_quality import service as dq

# ── bands ────────────────────────────────────────────────────────────
#: At or above this the CRM is being kept properly.
GOOD_AT = 85
#: At or above this it needs attention; below it, it is not being kept.
WATCH_AT = 65

#: How many offending records each factor carries with the score itself.
#: The rest are one click away, on the factor's own page.
SAMPLE = 5


class OutsideScope(Exception):
    """Asked for a person or a vertical the viewer may not see."""


# ── populations ──────────────────────────────────────────────────────
# What each factor is measured against. These deliberately call the Data
# Quality module's own helpers rather than re-writing the filters: the
# numerator comes from those checks, so the denominator has to come from
# the same idea of "open", "live" and "won" or the percentage lies.

def _p_live_leads(sc):
    from app.access import scope
    return scope.leads(dq._live_leads(), sc).count()


def _p_open_leads(sc):
    from app.access import scope
    return scope.leads(dq._open_leads(), sc).count()


def _p_won_leads(sc):
    from app import Lead
    from app.access import scope
    return scope.leads(dq._live_leads().filter(Lead.stage == 'Won'),
                       sc).count()


def _p_quote_stage_leads(sc):
    from app.access import scope
    from app.services.lead_value import OPEN_QUOTE_STAGES
    from app import Lead
    return scope.leads(
        dq._live_leads().filter(Lead.stage.in_(OPEN_QUOTE_STAGES)),
        sc).count()


def _p_all_opps(sc):
    from app import Opportunity
    from app.access import scope
    return scope.opportunities(Opportunity.query, sc).count()


def _p_open_opps(sc):
    from app.access import scope
    return scope.opportunities(dq._open_opps(), sc).count()


def _p_won_opps(sc):
    from sqlalchemy import or_
    from app import Opportunity
    from app.access import scope
    return scope.opportunities(Opportunity.query.filter(
        or_(Opportunity.stage.in_(defs.OPP_WON),
            Opportunity.won_at.isnot(None)),
        Opportunity.lost_at.is_(None)), sc).count()


def _p_lost_opps(sc):
    from app import Opportunity
    from app.access import scope
    return scope.opportunities(
        Opportunity.query.filter(Opportunity.stage.in_(defs.OPP_LOST)),
        sc).count()


def _p_accounts(sc):
    from app.access import scope
    return scope.companies(dq._active_companies(), sc).count()


def _p_open_rfqs(sc):
    from app.services import sales_rules as rules
    return rules.open_rfqs(sc=sc).count()


def _p_handovers(sc):
    from sqlalchemy import func
    from app.access import scope
    from app.models.tms_handover import HandoverStatus, WonHandover
    return scope.handovers(WonHandover.query.filter(
        func.coalesce(WonHandover.status, '') != HandoverStatus.CANCELLED),
        sc).count()


POPULATIONS = {
    'live_leads': ('live leads', _p_live_leads),
    'open_leads': ('open leads', _p_open_leads),
    'won_leads': ('won leads', _p_won_leads),
    'quote_stage_leads': ('leads at a quoting stage', _p_quote_stage_leads),
    'all_opps': ('opportunities', _p_all_opps),
    'open_opps': ('open opportunities', _p_open_opps),
    'won_opps': ('won opportunities', _p_won_opps),
    'lost_opps': ('lost opportunities', _p_lost_opps),
    'accounts': ('active accounts', _p_accounts),
    'open_rfqs': ('RFQs awaiting a quote', _p_open_rfqs),
    'handovers': ('live handovers', _p_handovers),
}


# ── the factors ──────────────────────────────────────────────────────
# key, label, weight, the Data Quality checks it is computed from, what
# it is measured against, and why it matters. The weights total 100 and
# the Hygiene and Escalation guide quotes them, so a change here is a
# change to a published document.

def _f(key, label, weight, checks, populations, why):
    return {'key': key, 'label': label, 'weight': weight,
            'checks': tuple(checks), 'populations': tuple(populations),
            'why': why}


FACTORS = (
    _f('ownership', 'Every record has an owner', 20,
       ('unowned_leads', 'leads_of_leavers', 'unowned_opps',
        'opps_of_leavers', 'no_pic', 'accounts_of_leavers'),
       ('live_leads', 'all_opps', 'accounts'),
       'A lead, deal or account with no owner — or one left behind by '
       'somebody who has gone — appears in nobody\'s work, so no '
       'reminder, task or escalation ever reaches it. It is the one '
       'failure that makes every other factor unmeasurable, which is '
       'why it carries the most weight.'),

    _f('next_action', 'A next action is set', 12,
       ('leads_no_followup',), ('open_leads',),
       'An open lead with no follow-up date, or one whose date has '
       'already passed, has nothing to remind its owner. The next step '
       'then depends on somebody remembering it.'),

    _f('followup_completion', 'Follow-ups are completed', 12,
       ('stale_leads',), ('open_leads',),
       f'An open lead nobody has called or emailed for '
       f'{defs.NO_CONTACT_DAYS} days is a follow-up that was planned and '
       f'never made. Contact means an activity or an email, never a '
       f'field edit.'),

    _f('overdue_updates', 'Open deals are kept up to date', 12,
       ('stale_opps',), ('open_opps',),
       f'A deal past its expected close date, or untouched for '
       f'{defs.STALE_OPPORTUNITY_DAYS} days, is still being counted in '
       f'the forecast as though it were live.'),

    _f('rfq_quotation', 'RFQs reach a quotation', 12,
       ('rfq_no_quote', 'quoted_without_quote'),
       ('open_rfqs', 'quote_stage_leads'),
       'A customer who asked for a price by a date that has passed and '
       'never received one, or a lead moved to Quoted with no quote '
       'value or date recorded — which counts as nothing in quote '
       'ageing, quote-to-win and pipeline value alike.'),

    _f('close_date', 'Expected close dates are set', 8,
       ('opps_no_close_date',), ('open_opps',),
       'A deal with no expected close date is left out of every '
       'forecast by month, however real it is.'),

    _f('lost_reason', 'A loss says what was learned', 4,
       ('lost_no_competitor',), ('lost_opps',),
       'A deal recorded as lost with nothing to say who won it teaches '
       'nobody anything. This reads the CRM\'s existing check on lost '
       'deals; it is the smallest weight because the record is already '
       'closed and the cost is only the learning.'),

    _f('won_handover', 'Won business is valued and handed over', 12,
       ('won_no_po', 'won_no_handover', 'won_no_value',
        'won_leads_no_value'),
       ('handovers', 'won_opps', 'won_leads'),
       'Won work with no customer PO cannot become a project, won work '
       'with no handover leaves operations nothing to act on, and won '
       'work with no value counts as zero in every report of what the '
       'business actually won.'),

    _f('account_contact', 'Accounts are contacted', 8,
       ('inactive_customers',), ('accounts',),
       f'An account with no lead, deal or conversation for '
       f'{defs.INACTIVE_CUSTOMER_MONTHS} months is a customer somebody '
       f'else is already serving.'),
)

BY_KEY = {f['key']: f for f in FACTORS}

#: The weights are a published figure, not an accident of editing.
assert sum(f['weight'] for f in FACTORS) == 100, 'hygiene weights must total 100'


def weights():
    """{factor key: weight} — for the guide and for the tests."""
    return {f['key']: f['weight'] for f in FACTORS}


def band_for(score):
    if score >= GOOD_AT:
        return 'good'
    if score >= WATCH_AT:
        return 'watch'
    return 'poor'


# ── narrowing the scope ──────────────────────────────────────────────
def narrow(sc, emp_code=None, vertical=None):
    """`sc` confined to one person or one vertical. Never wider.

    Refuses rather than silently returning nothing, so a manager can
    tell "they have nothing outstanding" apart from "not yours to see".
    """
    from app.access.scope import Scope

    code = (emp_code or '').strip().upper()
    name = (vertical or '').strip()
    if code:
        if not sc.unrestricted and not sc.reaches(code):
            raise OutsideScope('That person is outside your access')
        return Scope(sc.emp_code, sc.vertical, {code}, sc.perms,
                     sc.data_scope)
    if name:
        from app import Employee
        codes = {e.emp_code for e in Employee.query.filter(
            Employee.is_active.is_(True), Employee.vertical == name).all()
            if e.emp_code}
        if not sc.unrestricted:
            codes &= set(sc.codes or set())
        if not codes:
            raise OutsideScope('That vertical is outside your access')
        return Scope(sc.emp_code, name, codes, sc.perms, sc.data_scope)
    return sc


def levels(sc):
    """Which levels this viewer may ask for, widest last."""
    out = ['user']
    if sc.unrestricted or len(sc.codes or ()) > 1:
        out.append('vertical')
    if sc.unrestricted:
        out.append('company')
    return out


def verticals_for(sc):
    """The verticals a viewer may score, from the employee master."""
    from app import Employee
    q = Employee.query.filter(Employee.is_active.is_(True))
    if not sc.unrestricted:
        q = q.filter(Employee.emp_code.in_(sc.codes or {''}))
    return sorted({(e.vertical or '').strip() for e in q.all()
                   if (e.vertical or '').strip()})


# ── the score ────────────────────────────────────────────────────────
def _count(key, sc):
    """One check's count inside a scope, or None if the check failed.

    A broken check must not invent a deduction: a factor that cannot be
    measured says so and costs nothing.
    """
    from app import db
    check = dq.find(key)
    if check is None:
        return None
    try:
        count, _detail = dq.run(check, sc)
        return int(count or 0)
    except Exception:
        # A failed statement poisons the ones after it on some backends.
        try:
            db.session.rollback()
        except Exception:
            pass
        return None


def _population(factor, sc):
    total, parts = 0, []
    for key in factor['populations']:
        label, fn = POPULATIONS[key]
        try:
            n = int(fn(sc) or 0)
        except Exception:
            from app import db
            try:
                db.session.rollback()
            except Exception:
                pass
            n = 0
        total += n
        parts.append({'key': key, 'label': label, 'count': n})
    return total, parts


def _records(key, sc, sample):
    """A few of the records one check flagged, each with a link."""
    if sample <= 0:
        return []
    try:
        data = dq.records_for(key, sc=sc, per_page=sample)
    except Exception:
        from app import db
        try:
            db.session.rollback()
        except Exception:
            pass
        return []
    if not data:
        return []
    rows = data.get('records') or [r for g in data.get('groups') or []
                                   for r in g.get('records') or []]
    out = []
    for r in rows[:sample]:
        out.append({'id': r.get('id'), 'kind': r.get('kind', ''),
                    'name': r.get('name', ''), 'meta': r.get('meta', ''),
                    'route': r.get('route', ''), 'check': key,
                    'check_label': (dq.find(key).label if dq.find(key)
                                    else key)})
    return out


def _suffix(emp_code=None, vertical=None):
    from urllib.parse import urlencode
    q = {}
    if emp_code:
        q['for'] = emp_code
    if vertical:
        q['vertical'] = vertical
    return ('?' + urlencode(q)) if q else ''


def score(sc, emp_code=None, vertical=None, *, with_records=True,
          sample=SAMPLE):
    """The hygiene score for a boundary, and the arithmetic behind it.

    ``sc``        the viewer's scope — what they are allowed to see.
    ``emp_code``  score one person inside it (the user level).
    ``vertical``  score one vertical inside it (the vertical level).
    Neither given, the score covers everything the viewer may see, which
    for an unrestricted viewer is the whole company.

    Returns
        {'score': 0-100, 'band': 'good|watch|poor', 'factors': [
            {'key', 'label', 'weight', 'lost', 'count', 'why', 'route',
             'population', 'share', 'checks', 'records'}, ...]}
    """
    inner = narrow(sc, emp_code=emp_code, vertical=vertical)
    suffix = _suffix(emp_code, vertical)
    factors, lost_total, measured = [], 0.0, True

    for f in FACTORS:
        population, parts = _population(f, inner)
        counts, flagged, broken = [], 0, False
        for key in f['checks']:
            check = dq.find(key)
            n = _count(key, inner)
            if n is None:
                broken = True
                counts.append({'key': key, 'label': key, 'count': None,
                               'route': ''})
                continue
            flagged += n
            counts.append({
                'key': key,
                'label': check.label if check else key,
                'severity': check.severity if check else '',
                'count': n,
                'route': f'/hygiene/factor/{f["key"]}{suffix}'})

        if broken:
            measured = False
        # Nothing to get wrong deducts nothing — no credit either way.
        share = 0.0 if population <= 0 else min(1.0, flagged / population)
        lost = round(f['weight'] * share, 1)
        lost_total += lost

        records = []
        if with_records and flagged:
            for key in f['checks']:
                left = sample - len(records)
                if left <= 0:
                    break
                records.extend(_records(key, inner, left))

        factors.append({
            'key': f['key'], 'label': f['label'], 'weight': f['weight'],
            'lost': lost, 'count': flagged, 'population': population,
            'population_parts': parts,
            'share': round(share, 4), 'why': f['why'],
            'route': f'/hygiene/factor/{f["key"]}{suffix}',
            'checks': counts, 'records': records,
            'measured': not broken})

    total = round(max(0.0, min(100.0, 100.0 - lost_total)), 1)
    return {
        'score': total,
        'band': band_for(total),
        'lost': round(lost_total, 1),
        'factors': factors,
        'level': ('user' if emp_code else 'vertical' if vertical
                  else 'company' if sc.unrestricted else 'scope'),
        'emp_code': (emp_code or '').strip().upper(),
        'vertical': (vertical or '').strip(),
        'good_at': GOOD_AT, 'watch_at': WATCH_AT,
        'complete': measured,
        'generated_at': datetime.utcnow().isoformat(timespec='seconds'),
    }


def factor_records(sc, key, emp_code=None, vertical=None, page=1,
                   per_page=None):
    """Every record behind one factor, check by check, inside the scope.

    This is what makes a deduction arguable: the number on the score and
    the rows on this page are produced by the same checks, so a person
    who disagrees with the score can look at exactly what caused it.
    """
    factor = BY_KEY.get(key)
    if factor is None:
        return None
    inner = narrow(sc, emp_code=emp_code, vertical=vertical)
    per_page = max(1, int(per_page or defs.PAGE_SIZE))
    page = max(1, int(page or 1))
    groups, total = [], 0
    for check_key in factor['checks']:
        check = dq.find(check_key)
        try:
            data = dq.records_for(check_key, sc=inner, page=page,
                                  per_page=per_page)
        except Exception:
            from app import db
            try:
                db.session.rollback()
            except Exception:
                pass
            data = None
        if data is None:
            groups.append({'key': check_key, 'label': check_key,
                           'count': None, 'records': [], 'error': True})
            continue
        rows = data.get('records') or [r for g in data.get('groups') or []
                                       for r in g.get('records') or []]
        total += int(data.get('count') or 0)
        groups.append({
            'key': check_key,
            'label': check.label if check else check_key,
            'severity': check.severity if check else '',
            'why': check.why if check else '',
            'suggestion': check.suggestion if check else '',
            'count': int(data.get('count') or 0),
            'pages': data.get('pages', 1),
            'truncated': bool(data.get('truncated')),
            'records': rows})
    population, parts = _population(factor, inner)
    share = 0.0 if population <= 0 else min(1.0, total / population)
    return {'key': factor['key'], 'label': factor['label'],
            'weight': factor['weight'], 'why': factor['why'],
            'count': total, 'population': population,
            'population_parts': parts,
            'lost': round(factor['weight'] * share, 1),
            'groups': groups, 'page': page, 'per_page': per_page,
            'emp_code': (emp_code or '').strip().upper(),
            'vertical': (vertical or '').strip()}


def team(sc, limit=100):
    """A score per person the viewer's scope reaches, worst first.

    Only for a viewer whose scope is wider than themselves; a rep gets
    their own score and nobody else's.
    """
    from app import Employee
    if not sc.unrestricted and len(sc.codes or ()) <= 1:
        return []
    q = Employee.query.filter(Employee.is_active.is_(True))
    if not sc.unrestricted:
        q = q.filter(Employee.emp_code.in_(sc.codes or {''}))
    rows = []
    for emp in q.order_by(Employee.name.asc()).limit(limit).all():
        s = score(sc, emp_code=emp.emp_code, with_records=False)
        rows.append({'emp_code': emp.emp_code,
                     'name': emp.name or emp.emp_code,
                     'vertical': emp.vertical or '',
                     'score': s['score'], 'band': s['band'],
                     'worst': max(s['factors'],
                                  key=lambda f: f['lost'])['label']
                     if any(f['lost'] for f in s['factors']) else ''})
    rows.sort(key=lambda r: r['score'])
    return rows
