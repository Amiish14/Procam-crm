"""
What is still missing before a vertical can run the chain.

CRM-FINISH-ALL. Read-only, always.

Six releases are built and switched on for a handful of people,
and most of what remains is not code: a coordinator nobody has
named, an SCM table with a gap in it, terms still marked DRAFT,
a mail switch nobody has turned on. Each of those fails quietly —
a step opens owned by nobody, a clarification is composed and not
sent — and the person who notices is the customer.

So this says, per vertical, what is set and what is not. One
implementation behind the script and the Admin panel, because a
readiness check that disagrees with itself is worse than none.

It reports only on verticals whose flags are actually on. A
vertical nobody is piloting has nothing to be ready for, and
listing it as MISSING teaches people to ignore the list.
"""
from __future__ import annotations

OK, MISSING, WARN = 'OK', 'MISSING', 'WARN'

#: The flags a vertical needs before any of this matters.
CHAIN_FLAGS = ('FEATURE_RFQ_CHAIN',)


def _check(key, state, detail, *, who='', fix=''):
    return {'key': key, 'state': state, 'detail': detail,
            'who': who, 'fix': fix}


def live_verticals():
    """Verticals with at least one active person the chain is on for.

    Read from who the flag actually reaches rather than from a
    list somebody maintains: the flag is the truth about who is
    piloting, and a second list would drift from it.
    """
    from app import Employee
    from app.services import flags
    from app.services.vertical_scope import master_name

    out = {}
    try:
        people = Employee.query.filter_by(is_active=True).all()
    except Exception:                                     # noqa: BLE001
        return out
    for emp in people:
        if not any(flags.on_for(f, emp.emp_code)
                   for f in CHAIN_FLAGS):
            continue
        raw = (getattr(emp, 'vertical', '') or '').strip()
        if not raw or raw.lower() in ('all', 'management'):
            # Somebody who spans everything tells us nothing about
            # which vertical is being piloted.
            continue
        name = master_name(raw) or raw
        out.setdefault(name, []).append(emp.emp_code)
    return out


# ── the company-wide checks ─────────────────────────────────────────
def company_checks():
    from app.services import admin_settings, flags

    out = []

    coordinator = (admin_settings.get('rfq_coordinator') or '').strip()
    out.append(_check(
        'rfq_coordinator',
        OK if coordinator else MISSING,
        f'{coordinator} registers RFQs' if coordinator
        else 'Nobody registers RFQs — step 2 opens owned by nobody',
        who='a director', fix='scripts/set_rfq_chain_people.py'))

    external = _external_allowed()
    out.append(_check(
        'CRM_EMAIL_ALLOW_EXTERNAL',
        OK if external else MISSING,
        'The CRM may mail customers' if external
        else ('Customer mail is blocked: the screening '
              'clarification is composed and never sent, and '
              'quotations cannot go out from here'),
        who='whoever owns the server .env',
        fix='set CRM_EMAIL_ALLOW_EXTERNAL=true and restart'))

    blocked = flags.on('FEATURE_SENDER_BLOCK')
    out.append(_check(
        'FEATURE_SENDER_BLOCK',
        OK if blocked else WARN,
        'System senders are held, not made into leads' if blocked
        else ('Off — quarantine digests and bounces will become '
              'leads again'),
        who='a director',
        fix='turn the flag on in Admin → Feature flags'))

    out.append(_drafts())
    return out


def _external_allowed():
    try:
        from app.services import mail_policy
        return bool(mail_policy.external_allowed())
    except Exception:                                     # noqa: BLE001
        return False


def _drafts():
    """Term sets still carrying the DRAFT marker."""
    try:
        from app.models.quote_terms import QuoteTermSet
        rows = QuoteTermSet.query.filter_by(is_current=True).all()
    except Exception:                                     # noqa: BLE001
        return _check('quote_term_sets', MISSING,
                      'The terms library could not be read', who='')
    draft = [r.vertical for r in rows
             if 'draft' in (r.body or '')[:400].lower()]
    if not rows:
        return _check('quote_term_sets', MISSING,
                      'No terms on file for any vertical',
                      who='the commercial team',
                      fix='add a term set per vertical')
    if draft:
        return _check(
            'quote_term_sets', MISSING,
            f'{len(draft)} of {len(rows)} verticals still say '
            f'DRAFT: {", ".join(sorted(draft))}',
            who='the commercial team',
            fix='replace the placeholder clauses with real wording')
    return _check('quote_term_sets', OK,
                  f'{len(rows)} vertical(s) have real terms')


# ── per vertical ────────────────────────────────────────────────────
def vertical_checks(vertical, people=()):
    from app.services import admin_settings

    out = []
    for key, label in (('rfq_scm_by_vertical', 'SCM'),
                       ('rfq_ops_by_vertical', 'Ops')):
        table = admin_settings.get(key) or {}
        got = (table.get(vertical) or '').strip() \
            if isinstance(table, dict) else ''
        out.append(_check(
            key, OK if got else MISSING,
            f'{label}: {got}' if got
            else (f'No {label} default for {vertical} — the '
                  f'{"screening and costing" if label == "SCM" else "technical and survey"} '
                  f'steps open owned by nobody'),
            who='the vertical head',
            fix='scripts/set_rfq_chain_people.py'))

    out.append(_targets(vertical, people))
    out.append(_queue(vertical))
    return out


def _targets(vertical, people):
    from app.models.rfq_target import RfqTarget
    from app.services import lead_journey as lj

    month = lj.month_key()
    try:
        rows = RfqTarget.query.filter_by(month=month,
                                         vertical=vertical).all()
    except Exception:                                     # noqa: BLE001
        rows = []
    have = {r.emp_code for r in rows}
    missing = [p for p in people if p not in have]
    if rows and not missing:
        return _check('rfq_targets', OK,
                      f'{len(rows)} target(s) set for {month}')
    if not rows:
        return _check(
            'rfq_targets', MISSING,
            f'No RFQ targets for {month} — the funnel has nothing '
            f'to measure against',
            who='a director', fix='Funnels → RFQ targets')
    return _check(
        'rfq_targets', MISSING,
        f'{len(rows)} set for {month}, none for '
        f'{", ".join(sorted(missing))}',
        who='a director', fix='Funnels → RFQ targets')


def _queue(vertical):
    """How many leads this vertical's head would open to."""
    try:
        from app.access.scope import Scope
        from app.services import account_gate
        if not account_gate.enabled('*none*') \
                and not _flag_on('FEATURE_ACCOUNT_FIRST'):
            return _check('account_check_queue', OK,
                          'Account first is off for this vertical')
        rows = account_gate.pending_leads(
            sc=Scope(emp_code='', vertical='', codes=None, perms=(),
                     data_scope='all'))
        mine = [l for l in rows
                if account_gate.vertical_of(l) == vertical]
    except Exception:                                     # noqa: BLE001
        return _check('account_check_queue', WARN,
                      'The queue could not be counted')
    if not mine:
        return _check('account_check_queue', OK,
                      'Nothing waiting')
    return _check(
        'account_check_queue',
        WARN if len(mine) < 25 else MISSING,
        f'{len(mine)} lead(s) waiting for a customer',
        who='the vertical head',
        fix='/accounts/check — confirm or create the account')


def _flag_on(name):
    from app.services import flags
    return flags.on(name)


def report():
    """{'company': [...], 'verticals': {name: [...]}, ...}."""
    live = live_verticals()
    verticals = {name: vertical_checks(name, people)
                 for name, people in sorted(live.items())}
    company = company_checks()

    everything = list(company) + [c for rows in verticals.values()
                                  for c in rows]
    return {
        'company': company,
        'verticals': verticals,
        'people': live,
        'missing': sum(1 for c in everything if c['state'] == MISSING),
        'warnings': sum(1 for c in everything if c['state'] == WARN),
        'ready': not any(c['state'] == MISSING for c in everything),
        'no_pilot': not live,
    }
