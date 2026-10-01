"""
Release 6 (Group L) — the Copilot intents over the newer services.

What this module is for
    The Workbench, the sales rules, the weekly review, the contact
    directory and the market-intelligence tables all became real
    services in Releases 4–6. Each of them already answers a question a
    salesperson asks out loud. This module makes the Copilot able to ask
    them, and nothing more: there is no second AI path here, no model is
    ever handed a query to write, and every handler follows the contract
    in `app/copilot/queries.py` — it takes an already-resolved Scope,
    reads only through `app/access/scope.py` and `app/access/records.py`,
    and returns a `Result`.

Why the handlers never call `scope.current()`
    Because the §16 RBAC suite runs every intent as every persona in one
    process. A handler that resolves its own scope would read the
    signed-in session instead, and the sweep that catches an unscoped
    query would quietly stop catching anything. `tests/
    test_copilot_new_intents.py` asserts the absence of that call
    textually, which is the only check that cannot be fooled by a
    handler that happens not to leak on the fixture's data.

Why every sibling import is optional
    Group L lands beside Groups J, K and M, and this file was written
    while they were still arriving — `app/intel/` had its models before
    it had `projects.py`, `app/review/` was a package docstring for a
    while. A Copilot that raises ImportError because a neighbouring
    stream has not merged, or because its migration has not run on this
    server, is a worse outcome than one that says "that source is not
    configured yet". So every reach outside this module goes through
    `_module()`, `_table()` or `_read()`, and each of these intents has
    three tiers: the sibling service, then the models it reads, then an
    honest empty answer naming the missing source.

Never an invented row
    Where a source is missing, or its table has not been created, the
    answer is `Result(empty=True)` with a note naming the source. It is
    never a plausible-looking list. §3.4 again: a confident wrong answer
    is the most damaging thing this system can produce, and market
    intelligence — a competitor's win, a vessel's port call — is exactly
    the kind of fact nobody in the room can check on the spot.
"""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta

from app.copilot.intents import Result, intent
from app.copilot.queries import (ROW_CAP, _cap, _money, _nothing_recorded,
                                 _pick_company)

#: Returned by `_read()` when the read could not happen at all — a table
#: that was never created, a model that will not import. Deliberately
#: distinct from an empty list, because "nobody has recorded one" and
#: "this deployment cannot answer that" are different answers and the
#: second must say so.
_UNAVAILABLE = object()

#: "More than N days old" means this when the question does not say.
DEFAULT_RFQ_AGE_DAYS = 5
#: A week, for the weekly review's look-back.
REVIEW_WINDOW_DAYS = 7


# ── reaching the sibling streams, safely ─────────────────────────────
def _module(dotted, *required):
    """A sibling service module, or None when it is not installed.

    `required` names the callables this caller needs. A module that
    exists but has not grown the function yet is treated as absent,
    which is what lets this file be written against a service whose
    first half has merged and second half has not.
    """
    try:
        mod = importlib.import_module(dotted)
    except Exception:
        return None
    for name in required:
        if not callable(getattr(mod, name, None)):
            return None
    return mod


def _read(fn, default=_UNAVAILABLE):
    """Run a database read that may be against a table nobody created.

    The intel and directory tables arrive with their own stream's
    migration. Until it has run, SQLAlchemy raises OperationalError and
    — the part that matters — leaves the session in a failed state, so
    every later query in the same request fails too. Rolling back here
    keeps one unconfigured source from breaking the rest of the answer.
    """
    try:
        return fn()
    except Exception:
        try:
            from app import db
            db.session.rollback()
        except Exception:
            pass
        return default


def _table(name):
    """Whether a sibling stream's table has actually been created.

    Asked before the model is imported, not after. Importing a model
    registers it on the shared metadata, and these ones carry a foreign
    key to a `projects` table that does not exist yet — so a module that
    imported them speculatively would break the next `create_all()` for
    everybody. `app/access/records.py` guards the team tables the same
    way and for the same reason.
    """
    try:
        from app import db
        return bool(db.inspect(db.engine).has_table(name))
    except Exception:
        return False


def _not_configured(label, source, *, alternative=''):
    """The honest answer when a source is not installed or not migrated."""
    notes = [f'{source} is not configured in this deployment, so there is '
             f'nothing to read. No figure is given rather than a guess.']
    if alternative:
        notes.append(alternative)
    return Result(headline=f'I cannot see any {label} — {source} is not set '
                           f'up here.',
                  empty=True, notes=notes)


def _nothing_captured(label, source):
    """The source is installed; nobody has put anything in it yet."""
    return Result(headline=f'No {label} have been recorded yet.',
                  empty=True, sources=[source],
                  notes=[f'{source} exists but holds no rows, so there is '
                         f'nothing to report.'])


def _days(params, default):
    try:
        n = int(params.get('days') or default)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _chip(kind, ident, label):
    return {'type': kind, 'id': ident, 'label': label}


# ══════════════════════════════════════════════════════════════════════
#  THE DAY'S WORK  ·  the Workbench board, asked as a question
# ══════════════════════════════════════════════════════════════════════
#
# Everything below is phase 2 or later, deliberately. Phase 1 is §5's
# core, and `suggestions()` shows the first ten intents by (phase,
# label) — so a release that files nine new intents under phase 1
# silently evicts "Pipeline value" and "Stale leads" from the chips
# every salesperson actually clicks. The phase number is the lever for
# that, and these questions are genuinely the later release.
@intent('what_to_update_today', 'What to update today',
        params={'group': 'one Workbench group key, e.g. data_update'},
        personas=('sales', 'head'), phase=2,
        examples=('what do I need to update today',
                  'what needs updating today',
                  'what is on my workbench'))
def what_to_update_today(scope, params):
    """The Daily Workbench, read out loud.

    No permission: the board is already confined to the records the
    viewer owns or supervises, so the answer is the §6.6 safe form by
    construction. Nothing is recomputed here — the conditions, the
    priorities and the ordering all come from `app/workbench/service.py`
    so that the panel and the screen can never disagree about what today
    looks like, which is the one way this answer could do real harm.
    """
    board_mod = _module('app.workbench.service', 'board')
    if board_mod is None:
        return _not_configured('work for today', 'The Daily Workbench')

    group = (params.get('group') or '').strip() or None
    data = _read(lambda: board_mod.board(scope, group=group,
                                         per_page=ROW_CAP, sort='priority'))
    if data is _UNAVAILABLE:
        return _not_configured('work for today', 'The Daily Workbench')

    summary = data.get('summary') or {}
    total = int(data.get('total') or 0)
    if not total:
        return Result(headline='Nothing on your Workbench needs updating '
                               'today.',
                      figures={k: int(v or 0) for k, v in summary.items()},
                      empty=True, sources=['Daily Workbench'])

    rows = []
    for item in (data.get('items') or []):
        row = {
            'What': item.get('title') or '—',
            'Account': item.get('account') or '—',
            'Why': ', '.join(item.get('reasons') or []) or '—',
            'Due': item.get('due') or '—',
            'Stage': item.get('stage') or '—',
            'Owner': item.get('assigned_name') or item.get('assigned_to')
                     or '—',
        }
        # Only the kinds the panel knows how to open get a chip; a chip
        # pointing at a record type the UI cannot route to is a dead link.
        if item.get('kind') == 'lead' and item.get('id'):
            row['_chip'] = _chip('lead', item['id'], row['Account'])
        elif item.get('kind') == 'account' and item.get('account_id'):
            row['_chip'] = _chip('company', item['account_id'], row['Account'])
        rows.append(row)

    rows, notes = _cap(rows, total)
    headline = (f"{total} thing(s) need you today — "
                f"{int(summary.get('overdue') or 0)} overdue, "
                f"{int(summary.get('due_today') or 0)} due today, "
                f"{int(summary.get('data_updates') or 0)} needing a data "
                f"update.")
    return Result(
        headline=headline,
        columns=['What', 'Account', 'Why', 'Due', 'Stage', 'Owner'],
        rows=rows,
        figures={k: int(v or 0) for k, v in summary.items()},
        notes=notes,
        filters=({'group': group} if group else {}),
        sources=['Daily Workbench', 'app/services/sales_rules.py'])


# ══════════════════════════════════════════════════════════════════════
#  QUOTE AND RFQ AGEING  ·  the sales rules, asked as a question
# ══════════════════════════════════════════════════════════════════════
def _ageing(scope):
    """The shared quote-ageing read, or `_UNAVAILABLE`."""
    rules = _module('app.services.sales_rules', 'rfq_ageing')
    if rules is None:
        return _UNAVAILABLE
    return _read(lambda: rules.rfq_ageing(sc=scope))


@intent('quotes_overdue', 'Overdue quotations',
        permission='module.quotes',
        personas=('sales', 'head'), phase=2,
        examples=('show overdue quotations', 'overdue quotations',
                  'which quotations are overdue'))
def quotes_overdue(scope, params):
    """RFQs whose quote is past the date we are judged on.

    "Overdue" is `sales_rules.quote_due_date` — the customer's own
    deadline where one was committed, otherwise the service standard of
    `QUOTE_SLA_DAYS` after the RFQ arrived. That distinction is carried
    into the rows, because telling a salesperson they have broken a
    promise when the deadline was assumed by the CRM is how people stop
    believing the screen.
    """
    from app.access import scope as sc_mod

    ageing = _ageing(scope)
    if ageing is _UNAVAILABLE:
        return _not_configured('overdue quotations', 'The quote ageing rules')

    if not ageing['total'] and not _read(
            lambda: sc_mod.rfqs(sc=scope).count(), 0):
        return _nothing_recorded(
            'RFQs',
            'Quote ageing is measured from the RFQ register, which has no '
            'records yet. Leads at the "Quoted" stage are a separate count.')

    overdue = ageing.get('overdue') or []
    if not overdue:
        return Result(
            headline=(f'No quotation is past its deadline — '
                      f'{ageing["total"]} RFQ(s) are awaiting a quote.'),
            empty=True,
            figures={'overdue': 0, 'awaiting_quote': ageing['total']},
            sources=['app/services/sales_rules.py quote ageing'])

    overdue = sorted(overdue, key=lambda r: -(r.get('days_late') or 0))
    rows = [{
        'RFQ': r.get('rfq_number') or f"#{r.get('rfq_id')}",
        'Subject': r.get('subject') or '—',
        'Quote due': r.get('quote_due') or '—',
        'Days late': r.get('days_late') or 0,
        'Deadline': ('committed to the customer'
                     if r.get('deadline_committed') else 'our own standard'),
        'Owner': r.get('owner') or '—',
        '_chip': _chip('rfq', r.get('rfq_id'),
                       r.get('rfq_number') or 'RFQ'),
    } for r in overdue]
    capped, notes = _cap(rows, len(rows))
    worst = overdue[0].get('days_late') or 0
    return Result(
        headline=(f'{len(rows)} quotation(s) past the deadline, the worst '
                  f'by {worst} day(s).'),
        columns=['RFQ', 'Subject', 'Quote due', 'Days late', 'Deadline',
                 'Owner'],
        rows=capped,
        figures={'overdue': len(rows), 'worst_days_late': worst,
                 'awaiting_quote': ageing['total']},
        notes=notes,
        sources=['app/services/sales_rules.py quote ageing',
                 'rfqs.quote_by_date'])


@intent('rfqs_older_than', 'RFQs older than N days',
        permission='module.rfq',
        params={'days': 'minimum age in days (default 5)'},
        personas=('sales', 'head'), phase=2,
        examples=('which RFQs are more than 5 days old',
                  'RFQs older than 10 days', 'RFQ ageing'),
        )
def rfqs_older_than(scope, params):
    """RFQs still awaiting a quote, by age rather than by deadline.

    Age and lateness are different questions and the brief asks both.
    An RFQ three days old with a two-day deadline is late; one ten days
    old with no deadline at all is not late and is still the one that
    loses the job. The buckets come from `sales_rules.AGEING_BUCKETS`,
    so this answer and the Workbench's ageing strip count the same rows.
    """
    from app.access import scope as sc_mod

    days = _days(params, DEFAULT_RFQ_AGE_DAYS)
    ageing = _ageing(scope)
    if ageing is _UNAVAILABLE:
        return _not_configured('RFQ ageing', 'The quote ageing rules')

    if not ageing['total'] and not _read(
            lambda: sc_mod.rfqs(sc=scope).count(), 0):
        return _nothing_recorded(
            'RFQs',
            'Nothing is in the RFQ register, so there is no ageing to '
            'report.')

    everything = [r for rows in ageing['buckets'].values() for r in rows]
    found = sorted((r for r in everything if (r.get('age_days') or 0) > days),
                   key=lambda r: -(r.get('age_days') or 0))
    if not found:
        return Result(
            headline=(f'No RFQ awaiting a quote is more than {days} day(s) '
                      f'old — {ageing["total"]} are open.'),
            empty=True, filters={'days': days},
            figures={'count': 0, 'awaiting_quote': ageing['total']},
            sources=['app/services/sales_rules.py quote ageing'])

    rows = [{
        'RFQ': r.get('rfq_number') or f"#{r.get('rfq_id')}",
        'Subject': r.get('subject') or '—',
        'Received': r.get('received_date') or '—',
        'Age (days)': r.get('age_days'),
        'Quote due': r.get('quote_due') or '—',
        'Owner': r.get('owner') or '—',
        '_chip': _chip('rfq', r.get('rfq_id'),
                       r.get('rfq_number') or 'RFQ'),
    } for r in found]
    capped, notes = _cap(rows, len(rows))
    return Result(
        headline=(f'{len(rows)} RFQ(s) awaiting a quote are more than '
                  f'{days} day(s) old, the oldest '
                  f'{found[0].get("age_days")} day(s).'),
        columns=['RFQ', 'Subject', 'Received', 'Age (days)', 'Quote due',
                 'Owner'],
        rows=capped,
        figures={'count': len(rows), 'oldest_days': found[0].get('age_days'),
                 'awaiting_quote': ageing['total']},
        notes=notes, filters={'days': days},
        sources=['app/services/sales_rules.py quote ageing',
                 'rfqs.received_date'])


# ══════════════════════════════════════════════════════════════════════
#  ACCOUNTS
# ══════════════════════════════════════════════════════════════════════
@intent('accounts_not_contacted', 'Accounts I have not contacted',
        params={'days': 'days of silence (default 90)'},
        personas=('sales', 'acct'), phase=2,
        examples=('which accounts have I not contacted in 90 days',
                  'accounts I have not contacted',
                  'my accounts with no contact'))
def accounts_not_contacted(scope, params):
    """The account-development half of the Workbench, as a question.

    No permission, and that is deliberate rather than an oversight. The
    existing `accounts_inactive` intent is the management report over
    everybody's accounts and is gated on `reports.accounts`; this one is
    first-person and reaches no further than `scope.companies`, which is
    the viewer's own book. Gating a person out of a list of their own
    customers teaches them the Copilot is broken, which §6.2 is explicit
    about. The threshold is the Workbench's `ACCOUNT_QUIET_DAYS` so the
    board and the answer agree on what "quiet" means.
    """
    from app import Company
    from app.access import scope as sc_mod
    from sqlalchemy import or_

    rules = _module('app.services.sales_rules')
    default = getattr(rules, 'ACCOUNT_QUIET_DAYS', 90) if rules else 90
    days = _days(params, default)
    cutoff = datetime.utcnow() - timedelta(days=days)

    base = (sc_mod.companies(sc=scope)
            .filter(Company.is_active.is_(True))
            .filter(or_(Company.last_activity_at.is_(None),
                        Company.last_activity_at < cutoff)))
    total = _read(lambda: base.count(), _UNAVAILABLE)
    if total is _UNAVAILABLE:
        return _not_configured('accounts', 'The Account Master')
    if not total:
        return Result(
            headline=(f'Every account you hold has been contacted in the '
                      f'last {days} days.'),
            empty=True, filters={'days': days},
            figures={'count': 0},
            sources=['companies.last_activity_at'])

    found = (base.with_entities(
        Company.id, Company.name, Company.vertical, Company.pic_emp_code,
        Company.last_activity_at, Company.next_action_at)
        .order_by(Company.last_activity_at.asc().nullsfirst(),
                  Company.id.asc())
        .limit(ROW_CAP).all())
    today = datetime.utcnow().date()
    rows = []
    for c in found:
        last = c.last_activity_at
        quiet = (today - last.date()).days if last else None
        rows.append({
            'Account': c.name,
            'Vertical': c.vertical or '—',
            'Owner': c.pic_emp_code or '—',
            'Last contact': str(last or '')[:10] or 'never',
            'Quiet (days)': quiet if quiet is not None else '—',
            'Next action': str(c.next_action_at or '')[:10] or '—',
            '_chip': _chip('company', c.id, c.name),
        })
    rows, notes = _cap(rows, total)
    return Result(
        headline=f'{total} account(s) you hold have been quiet for {days}+ '
                 f'days.',
        columns=['Account', 'Vertical', 'Owner', 'Last contact',
                 'Quiet (days)', 'Next action'],
        rows=rows, figures={'count': total}, notes=notes,
        filters={'days': days},
        sources=['companies.last_activity_at',
                 'app/services/sales_rules.py ACCOUNT_QUIET_DAYS'])


@intent('who_handles_account', 'Who handles the account in hand',
        params={'account': 'the customer name', 'account_id': 'or its id'},
        personas=('sales', 'head', 'ops'), phase=2,
        examples=('who handles this account',
                  'who in our team handles this account',
                  'who looks after this customer'))
def who_handles_account(scope, params):
    """Routing, and only routing — safe at every scope.

    Deliberately unpermissioned for the reason §6.6 gives: withholding
    who owns a customer causes the duplicate approach the CRM exists to
    prevent. The directory service owns this answer once it lands, since
    it also knows the people behind the ownership; until then the
    Account Master's PIC columns answer it, which is where the directory
    reads from anyway.
    """
    from app import Employee

    company, ask = _pick_company(
        scope, params, ask=lambda n: f'who handles the {n} account')
    if ask is not None:
        return ask
    if company is None:
        named = (params.get('account') or '').strip()
        if named:
            return Result(headline=f'No account matching "{named}" in the '
                                   f'CRM.',
                          empty=True, sources=['companies.name'])
        return Result(headline='Which account do you mean?', empty=True,
                      notes=['Name the customer, or open its record and ask '
                             'again.'])

    # The directory answers this question for the whole CRM, including
    # the owners the Account Master does not carry (backup PIC) and when
    # the account was last touched. Its own docstring is explicit that
    # the routing answer is not scoped — so this is a reuse, not a
    # widening: `who_handles` decides what a reader without a claim on
    # the account gets, and that decision stays in one place.
    directory = _module('app.directory.service', 'who_handles')
    owners, touched, source = [], '', 'Account Master'
    if directory is not None:
        answer = _read(lambda: directory.who_handles(scope, company.name))
        if answer is not _UNAVAILABLE:
            for match in ((answer or {}).get('matches') or []):
                if match.get('account_id') != company.id:
                    continue
                owners = list(match.get('owners') or [])
                touched = match.get('last_interaction') or ''
                source = 'Contact directory'
                break

    if owners:
        who = ', '.join(
            f"{o.get('name') or o.get('emp_code') or '—'} "
            f"({o.get('role') or 'owner'})" for o in owners)
        headline = f'{company.name} — {who}'
    else:
        def named(code):
            if not code:
                return ''
            emp = Employee.query.filter_by(emp_code=code).first()
            return (emp.name if emp else code) or code

        primary = named(company.pic_emp_code)
        if not primary:
            return Result(
                headline=f'{company.name} is in the CRM but nobody is set '
                         f'as its PIC.',
                notes=['An account with no PIC cannot auto-assign its '
                       'leads.'],
                sources=['companies.pic_emp_code'])
        secondary = named(company.secondary_pic_emp_code)
        headline = f'{company.name} — handled by {primary}'
        if secondary:
            headline += f', with {secondary} as secondary'

    if company.vertical:
        headline += f' · {company.vertical}'
    notes = []
    if touched:
        notes.append(f'Last interaction recorded {str(touched)[:10]}.')
    if not scope.reaches(company.pic_emp_code or ''):
        notes.append('This account sits outside your data scope, so only '
                     'the routing is shown.')
    return Result(
        headline=headline + '.',
        figures={'primary': company.pic_emp_code or '',
                 'secondary': company.secondary_pic_emp_code or ''},
        notes=notes, sources=[source])


# ══════════════════════════════════════════════════════════════════════
#  THE WEEKLY REVIEW
# ══════════════════════════════════════════════════════════════════════
@intent('my_weekly_review', 'My weekly sales review',
        params={'days': 'the window to review (default 7)'},
        personas=('sales', 'head'), phase=2,
        examples=('prepare my weekly sales review', 'weekly sales review',
                  'my weekly review'))
def my_weekly_review(scope, params):
    """The review pack, from the review service where it exists.

    Unpermissioned: every figure in it is drawn from the viewer's own
    scope, so it discloses nothing they could not get by asking the
    questions one at a time.

    `review.meeting('emp:CODE')` is the agenda the review screen runs,
    so the panel and the meeting cannot disagree about what the week
    held. Only the section counts are read here, never recomputed.

    The fallback, for a deployment where the review service is not
    installed, is not a second definition of a weekly review. It is the
    Workbench's own summary plus the quote ageing, both already
    computed from `sales_rules`, and it says in a note that it is the
    fallback — because a review that quietly leaves out the sections it
    cannot see is worse than one that admits to it.
    """
    days = _days(params, REVIEW_WINDOW_DAYS)

    review = _module('app.review.service', 'meeting')
    if review is not None and scope.emp_code:
        end = datetime.utcnow().date()
        start = end - timedelta(days=days - 1)
        pack = _read(lambda: review.meeting(f'emp:{scope.emp_code}',
                                            sc=scope, start=start, end=end))
        if pack is not _UNAVAILABLE and pack:
            agenda = list(pack.get('agenda') or [])
            period = pack.get('period') or {}
            rows = [{
                'Agenda item': s.get('title') or s.get('key'),
                'Items': int(s.get('count') or 0),
                'Value': (_money(s['value_inr']) if s.get('value_inr')
                          else '—'),
                'Note': s.get('note') or '—',
            } for s in agenda]
            figures = {s.get('key'): int(s.get('count') or 0)
                       for s in agenda if s.get('key')}
            busiest = max(agenda, key=lambda s: int(s.get('count') or 0),
                          default=None)
            total = sum(figures.values())
            if not total:
                return Result(
                    headline=f'Nothing to review for '
                             f'{period.get("start", start)} to '
                             f'{period.get("end", end)} — the agenda is '
                             f'empty.',
                    empty=True, figures=figures, filters={'days': days},
                    sources=['Weekly sales review'])
            return Result(
                headline=(f'Your review for {period.get("start", start)} to '
                          f'{period.get("end", end)}: {total} item(s) across '
                          f'{len([s for s in agenda if s.get("count")])} '
                          f'agenda section(s)'
                          + (f', the largest being {busiest["title"]} '
                             f'({busiest["count"]}).' if busiest and
                             busiest.get('count') else '.')),
                columns=['Agenda item', 'Items', 'Value', 'Note'],
                rows=rows, figures=figures,
                filters={'days': days},
                sources=['Weekly sales review'])

    board_mod = _module('app.workbench.service', 'board')
    if board_mod is None:
        return _not_configured('weekly review', 'The weekly review service')
    data = _read(lambda: board_mod.board(scope, per_page=1))
    if data is _UNAVAILABLE:
        return _not_configured('weekly review', 'The weekly review service')

    summary = {k: int(v or 0) for k, v in (data.get('summary') or {}).items()}
    counts = data.get('group_counts') or {}
    rows = [{'Work group': g['label'], 'Items': g['count']}
            for g in (data.get('groups') or []) if g.get('count')]
    headline = (f"Your week: {summary.get('overdue', 0)} overdue, "
                f"{summary.get('quotes_overdue', 0)} quotation(s) past the "
                f"deadline, {summary.get('stale', 0)} going cold, "
                f"{summary.get('high_value', 0)} high-value priority(ies).")
    if not rows:
        return Result(
            headline='Your Workbench is clear, so there is nothing to review.',
            empty=True, figures=summary, filters={'days': days},
            notes=['The dedicated weekly review service is not installed '
                   'here; this is the Workbench summary instead.'],
            sources=['Daily Workbench'])
    return Result(
        headline=headline,
        columns=['Work group', 'Items'], rows=rows,
        figures={**summary, **{f'group_{k}': int(v or 0)
                               for k, v in counts.items()}},
        notes=['The dedicated weekly review service is not installed here, '
               'so this is built from the Workbench and the quote ageing — '
               'the same figures, not a second set.'],
        filters={'days': days},
        sources=['Daily Workbench', 'app/services/sales_rules.py'])


# ══════════════════════════════════════════════════════════════════════
#  MARKET INTELLIGENCE  ·  honest about an unconfigured source
# ══════════════════════════════════════════════════════════════════════
#
# Each of these prefers the intel stream's own service — `app.intel.
# projects`, `.competitors`, `.vendors` — and falls back to the intel
# models, which are the same rows those services read. Reading the
# model directly is not a second definition of anything: these are
# captured facts with no derived figure over them. It keeps the answers
# truthful on a server where the package has landed but its service
# layer has not, which is the state every mid-release deployment is in.

@intent('projects_with_epc', 'Tracked projects with an EPC appointed',
        permission='module.competitors',
        params={'limit': 'how many to list'},
        personas=('head', 'mgmt'), phase=3,
        examples=('which tracked projects appointed an EPC',
                  'projects with an EPC contractor',
                  'which projects have an EPC'))
def projects_with_epc(scope, params):
    """Tracked projects that have named their EPC contractor.

    The EPC appointment is the moment a project becomes a real logistics
    enquiry, which is why it is worth a question of its own: before it,
    nobody knows who will buy the freight.
    """
    projects = _module('app.intel.projects', 'listing')
    if projects is not None:
        # `listing` applies the project-intelligence service's own
        # visibility rule, which is not the same as a lead's — an
        # unowned project is public-source intelligence and visible to
        # everyone. Reusing it means this answer cannot drift from the
        # Project Intelligence screen.
        page = _read(lambda: projects.listing(scope, per_page=200))
        if page is not _UNAVAILABLE:
            items = [i for i in (page.get('items') or [])
                     if (i.get('intel') or {}).get('epc_contractor')]
            if not items:
                return _nothing_captured(
                    'tracked projects with an EPC appointed',
                    'Project intelligence')
            rows = [{
                'Project': i.get('name') or i.get('title') or '—',
                'Owner group': (i['intel'].get('owner_group')
                                or i.get('customer') or '—'),
                'EPC contractor': i['intel']['epc_contractor'],
                'Stage': i.get('stage_label') or i['intel'].get(
                    'intel_stage') or '—',
                'Value': _money(i['intel'].get('value_inr'))
                         if i['intel'].get('value_inr') else '—',
                'Confidence': i['intel'].get('confidence') or '—',
            } for i in items]
            capped, notes = _cap(rows, len(rows))
            return Result(
                headline=f'{len(rows)} tracked project(s) have appointed an '
                         f'EPC contractor.',
                columns=['Project', 'Owner group', 'EPC contractor', 'Stage',
                         'Value', 'Confidence'],
                rows=capped, figures={'count': len(rows)}, notes=notes,
                sources=['Project intelligence',
                         'intel_project_facts.epc_contractor'])

    facts = (_read(_epc_facts) if _table('intel_project_facts')
             else _UNAVAILABLE)
    if facts is _UNAVAILABLE:
        return _not_configured('tracked projects',
                               'Project intelligence (app/intel)')
    if not facts:
        return _nothing_captured('tracked projects with an EPC appointed',
                                 'Project intelligence')

    rows = [{
        'Project owner': f.owner_group or '—',
        'Location': f.location or '—',
        'EPC contractor': f.epc_contractor,
        'Stage': f.intel_stage or '—',
        'Value': _money(f.value_inr) if f.value_inr else '—',
        'Announced': str(f.announcement_date or '')[:10] or '—',
        'Confidence': f.confidence or '—',
    } for f in facts]
    rows, notes = _cap(rows, len(facts))
    return Result(
        headline=f'{len(facts)} tracked project(s) have appointed an EPC '
                 f'contractor.',
        columns=['Project owner', 'Location', 'EPC contractor', 'Stage',
                 'Value', 'Announced', 'Confidence'],
        rows=rows, figures={'count': len(facts)}, notes=notes,
        sources=['intel_project_facts.epc_contractor'])


def _epc_facts():
    from app.models.intel import IntelProjectFact
    return (IntelProjectFact.query
            .filter(IntelProjectFact.epc_contractor.isnot(None),
                    IntelProjectFact.epc_contractor != '',
                    IntelProjectFact.not_relevant.isnot(True))
            .order_by(IntelProjectFact.announcement_date.desc().nullslast(),
                      IntelProjectFact.id.desc())
            .limit(ROW_CAP * 2).all())


@intent('competitors_recent_projects', 'What competitors have been doing',
        permission='module.competitors',
        params={'days': 'how far back to look (default 180)',
                'vertical': 'only this service, e.g. Warehousing'},
        personas=('head', 'mgmt'), phase=3,
        examples=('which competitors recently executed warehouse projects',
                  'recent competitor projects',
                  'what have competitors been winning'))
def competitors_recent_projects(scope, params):
    """Competitor activity, newest first.

    Every row carries its confidence and its source, because competitor
    intelligence is the one dataset in this CRM where "somebody heard"
    and "the press release says" are routinely mistaken for each other.
    """
    days = _days(params, 180)
    term = (params.get('vertical') or '').strip()
    since = (datetime.utcnow() - timedelta(days=days)).date()

    service = _module('app.intel.competitors', 'search')
    if service is not None:
        page = _read(lambda: service.search(since=since, service=term,
                                            per_page=200))
        if page is _UNAVAILABLE and term:
            # The service axis is a Master Data code; a word the person
            # typed ("warehouse") may not be one. Falling back to the
            # unfiltered window and narrowing in Python is better than
            # reporting nothing happened.
            page = _read(lambda: service.search(since=since, per_page=200))
        if page is not _UNAVAILABLE:
            items = page.get('items') or []
            if term:
                low = term.lower()
                narrowed = [i for i in items
                            if low in ' '.join(
                                str(i.get(k) or '') for k in
                                ('service', 'vertical', 'industry',
                                 'summary')).lower()]
                items = narrowed or items
            if not items:
                return _nothing_captured(
                    f'competitor projects in the last {days} days',
                    'Competitor intelligence')
            rows = [{
                'Competitor': i.get('competitor_name') or '—',
                'What happened': (i.get('summary') or '')[:200],
                'Service': i.get('service') or i.get('vertical') or '—',
                'Where': i.get('geography') or '—',
                'When': i.get('event_date') or '—',
                'Confidence': i.get('confidence') or '—',
                'Source': i.get('source') or '—',
            } for i in items]
            capped, notes = _cap(rows, len(rows))
            return Result(
                headline=f'{len(rows)} competitor activity record(s) in the '
                         f'last {days} days.',
                columns=['Competitor', 'What happened', 'Service', 'Where',
                         'When', 'Confidence', 'Source'],
                rows=capped, figures={'count': len(rows)}, notes=notes,
                filters={'days': days, **({'service': term} if term else {})},
                sources=['Competitor intelligence'])

    found = (_read(lambda: _competitor_activity(days,
                                                params.get('vertical')))
             if _table('intel_competitor_activity') else _UNAVAILABLE)
    if found is _UNAVAILABLE:
        return _not_configured('competitor activity',
                               'Competitor intelligence (app/intel)')
    if not found:
        return _nothing_captured(
            f'competitor projects in the last {days} days',
            'Competitor intelligence')

    rows = [{
        'Competitor': a.competitor_name,
        'What happened': (a.summary or '')[:200],
        'Service': a.service or a.vertical or '—',
        'Where': a.geography or '—',
        'When': str(a.event_date or '')[:10] or '—',
        'Confidence': a.confidence or '—',
        'Source': a.source or '—',
    } for a in found]
    rows, notes = _cap(rows, len(found))
    return Result(
        headline=f'{len(found)} competitor activity record(s) in the last '
                 f'{days} days.',
        columns=['Competitor', 'What happened', 'Service', 'Where', 'When',
                 'Confidence', 'Source'],
        rows=rows, figures={'count': len(found)}, notes=notes,
        filters={'days': days},
        sources=['intel_competitor_activity'])


def _competitor_activity(days, vertical):
    from app.models.intel import IntelCompetitorActivity as Act
    from sqlalchemy import or_
    since = (datetime.utcnow() - timedelta(days=days)).date()
    q = Act.query.filter(or_(Act.event_date.is_(None),
                             Act.event_date >= since))
    term = (vertical or '').strip()
    if term:
        like = f'%{term}%'
        q = q.filter(or_(Act.service.ilike(like), Act.vertical.ilike(like),
                         Act.industry.ilike(like), Act.summary.ilike(like)))
    return (q.order_by(Act.event_date.desc().nullslast(), Act.id.desc())
            .limit(ROW_CAP * 2).all())


@intent('vessel_operators_calling', 'Operators calling a port',
        params={'port': 'the Indian port, e.g. Chennai',
                'vessel_type': 'e.g. MPV, breakbulk, heavy-lift'},
        personas=('ops', 'sales'), phase=3,
        examples=('which MPV operators call Chennai',
                  'which operators call at Mundra',
                  'MPV operators calling India'))
def vessel_operators_calling(scope, params):
    """Which operators bring the right ships to a port.

    No permission: this is shipping-market reference data with no
    customer record in it, so there is nothing here that one colleague
    may see and another may not. Scope would narrow nothing and would
    only make the answer wrong for whoever asked.
    """
    # `account` is where the routing rules put the port: the classifier
    # has one name extractor and it is called `account`. Giving the
    # rules a `_capture: 'port'` of their own is a one-line change in
    # service.py, which this release may not make; until then the port
    # arrives under the only name the extractor knows.
    port = (params.get('port') or params.get('account') or '').strip()
    kind = (params.get('vessel_type') or '').strip()

    vendors = _module('app.intel.vendors', 'port_calls', 'search_vessels',
                      'ports')
    if vendors is not None:
        rows = _read(lambda: _operators_from_registers(vendors, port, kind))
        if rows is not _UNAVAILABLE:
            if not rows:
                where = f' calling {port}' if port else ''
                return _nothing_captured(
                    f'{kind or "vessel"} operators{where}',
                    'The vessel and port-call registers')
            capped, notes = _cap(rows, len(rows))
            return Result(
                headline=(f'{len(rows)} operator(s) recorded'
                          + (f' calling {port}' if port else '')
                          + (f' with {kind} tonnage' if kind else '') + '.'),
                columns=['Operator', 'Vessel', 'Type', 'Port', 'Window',
                         'Agent'],
                rows=capped, figures={'count': len(rows)}, notes=notes,
                filters={k: v for k, v in (('port', port),
                                           ('vessel_type', kind)) if v},
                sources=['Vessel register', 'India port calls'])

    found = (_read(lambda: _operators(port, kind))
             if _table('vessel_port_calls') and _table('vessels')
             else _UNAVAILABLE)
    if found is _UNAVAILABLE:
        return _not_configured('vessel operators',
                               'Vessel and port-call intelligence '
                               '(app/intel)')
    if not found:
        where = f' calling {port}' if port else ''
        return _nothing_captured(f'{kind or "vessel"} operators{where}',
                                 'Vessel and port-call intelligence')

    rows, notes = _cap(found, len(found))
    return Result(
        headline=(f'{len(found)} operator(s) recorded'
                  + (f' calling {port}' if port else '')
                  + (f' with {kind} tonnage' if kind else '') + '.'),
        columns=['Operator', 'Vessel', 'Type', 'Port', 'Last call',
                 'Local agent'],
        rows=rows, figures={'count': len(found)}, notes=notes,
        filters={k: v for k, v in (('port', port), ('vessel_type', kind))
                 if v},
        sources=['vessels', 'vessel_port_calls'])


def _port_code(vendors, port):
    """The Master Data code for a port somebody named in words.

    "Chennai" is a label; `port_calls` filters on `INMAA`. Returning ''
    for an unknown word is deliberate — a wrong code would silently
    answer about the wrong port, and no filter at least answers about
    all of them and says so.
    """
    text = (port or '').strip().lower()
    if not text:
        return ''
    for code, label in (vendors.ports() or []):
        if text == (code or '').lower() or text == (label or '').lower():
            return code
    for code, label in (vendors.ports() or []):
        if text in (label or '').lower():
            return code
    return ''


def _operators_from_registers(vendors, port, kind):
    """Operators, from the vendor stream's own two registers.

    One read of the port calls and, when a vessel type was asked for,
    one of the vessel register to decide which calls qualify — rather
    than a query per call.
    """
    code = _port_code(vendors, port)
    calls = (vendors.port_calls(port_code=code, per_page=200).get('items')
             or [])
    if port and not code:
        # Narrow on the name as typed, since the code was not known.
        low = port.lower()
        calls = [c for c in calls
                 if low in (c.get('port_name') or '').lower()]

    types = {}
    if kind:
        fleet = (vendors.search_vessels(vessel_type=kind, per_page=200)
                 .get('items') or [])
        allowed_ids = {v['id'] for v in fleet}
        allowed_names = {(v.get('name') or '').lower() for v in fleet}
        types = {v['id']: v.get('vessel_type') or '' for v in fleet}
        calls = [c for c in calls
                 if c.get('vessel_id') in allowed_ids
                 or (c.get('vessel_name') or '').lower() in allowed_names]

    seen, rows = set(), []
    for c in calls:
        operator = (c.get('operator') or '').strip()
        if not operator:
            continue
        key = (operator.lower(), (c.get('vessel_name') or '').lower())
        if key in seen:
            continue
        seen.add(key)
        window = ' → '.join(x for x in (c.get('eta'), c.get('etd')) if x)
        rows.append({
            'Operator': operator,
            'Vessel': c.get('vessel_name') or '—',
            'Type': types.get(c.get('vessel_id'), kind or '—') or '—',
            'Port': c.get('port_name') or c.get('port_code') or '—',
            'Window': window or '—',
            'Agent': c.get('agent') or '—',
        })
    return rows


def _operators(port, kind):
    """Operator rows from the port-call log, widened by vessel capability."""
    from sqlalchemy import or_
    from app.models.intel import Vessel, VesselPortCall

    calls = VesselPortCall.query
    if port:
        like = f'%{port}%'
        calls = calls.filter(or_(VesselPortCall.port_name.ilike(like),
                                 VesselPortCall.port_code.ilike(like)))
    calls = (calls.order_by(VesselPortCall.eta.desc().nullslast(),
                            VesselPortCall.id.desc())
             .limit(ROW_CAP * 4).all())

    wanted = (kind or '').strip().lower().replace('-', '_').replace(' ', '_')
    seen, rows = set(), []
    for call in calls:
        vessel = None
        if call.vessel_id:
            vessel = Vessel.query.get(call.vessel_id)
        if wanted:
            vtype = (getattr(vessel, 'vessel_type', '') or '').lower()
            heavy = bool(getattr(vessel, 'heavy_lift', False))
            if wanted not in vtype and not (
                    wanted in ('heavy_lift', 'heavylift') and heavy):
                continue
        operator = (call.operator or getattr(vessel, 'owner_operator', '')
                    or '').strip()
        if not operator:
            continue
        key = (operator.lower(), (call.vessel_name or '').lower())
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            'Operator': operator,
            'Vessel': call.vessel_name or '—',
            'Type': (getattr(vessel, 'vessel_type', '') or '—'),
            'Port': call.port_name or call.port_code or '—',
            'Last call': str(call.eta or '')[:10] or '—',
            'Local agent': (call.agent
                            or getattr(vessel, 'local_agent', '') or '—'),
        })
    return rows


@intent('contacts_at_company', 'Contacts we know at this company',
        params={'account': 'the company name', 'account_id': 'or its id'},
        personas=('sales', 'acct'), phase=3,
        examples=('which contacts do we know at this company',
                  'who do we know at this company',
                  'which contacts do we know there'))
def contacts_at_company(scope, params):
    """The directory's answer for one company.

    This is the directory's question, not the CRM's: `key_contacts`
    already answers "who is the contact at X" from `contacts`, and
    duplicating it here with a different definition would put two
    different lists of the customer's people on the same screen. So
    when the directory service is not installed, this says so and points
    at the intent that can answer from the CRM's own records — rather
    than inventing a third answer.
    """
    company, ask = _pick_company(
        scope, params,
        ask=lambda n: f'which contacts do we know at {n}')
    if ask is not None:
        return ask

    missing = _not_configured(
        'directory contacts', 'The contact directory (app/directory)',
        alternative='Ask "who is the contact at <customer>" to read the '
                    'contacts the CRM itself holds.')
    directory = _module('app.directory.service', 'search_contacts')
    if directory is None:
        return missing

    if company is None:
        return Result(headline='Which company do you mean?', empty=True,
                      notes=['Name the company, or open its record and ask '
                             'again.'])

    # `search_contacts` is already scoped — `base_query(sc)` confines it
    # to the contacts this viewer may see — so the account id is a
    # filter, never a way round the boundary.
    page = _read(lambda: directory.search_contacts(
        scope, account_id=company.id, per_page=ROW_CAP))
    if page is _UNAVAILABLE:
        return missing
    items = page.get('items') or []
    if not items:
        return Result(headline=f'The directory holds no contacts at '
                               f'{company.name} that you may see.',
                      empty=True, sources=['Contact directory'])

    rows = [{
        'Name': c.get('name') or '—',
        'Designation': c.get('designation') or '—',
        'Email': c.get('email') or '—',
        'Phone': c.get('phone') or '—',
        'Relationship': c.get('relationship') or '—',
        'Last interaction': c.get('last_interaction') or 'never',
        '_chip': _chip('contact', c.get('id'), c.get('name') or 'Contact'),
    } for c in items]
    total = int(page.get('total') or len(rows))
    capped, notes = _cap(rows, total)
    return Result(
        headline=f'{total} contact(s) known at {company.name}.',
        columns=['Name', 'Designation', 'Email', 'Phone', 'Relationship',
                 'Last interaction'],
        rows=capped, figures={'count': total}, notes=notes,
        sources=['Contact directory'])



# ══════════════════════════════════════════════════════════════════════
#  ROUTING
# ══════════════════════════════════════════════════════════════════════
#
# The rules that reach these intents. They belong in
# `app/copilot/service.py::_PATTERNS`, which this release may not edit,
# so they are declared here and inserted at the FRONT of that list by
# `install_patterns()` below. The front is not a convenience: several of
# the existing rules are general enough to swallow these questions —
# "who handles …" is already `account_owner`, "accounts … not contacted"
# is already `accounts_inactive` — and first match wins. Each rule here
# is written tightly enough that it claims only its own phrasings, which
# `tests/test_copilot_library.py` proves by re-running every existing
# library question through the classifier.
#
# Matching runs against the SYNONYM-EXPANDED question, which appends
# expansions to the end of the text ("rfq" adds "request for
# quotation"). So no rule here spans `.*` from a word to a word that the
# expansion could have appended — "overdue … quotation" would otherwise
# match "overdue RFQs". Adjacency is used instead.

#: "<kind> operators [calling] <port>". `_group: True` makes the rule
#: run on the words as typed, and hands whatever `account` captured to
#: the handler — which reads it as the port. The group has to be called
#: `account` because that is the only name `_group_name` looks for.
_VESSEL_RX = (
    r'\b(?:{kind})\s+(?:vessel\s+|ship\s+)?'
    r'(?:operators?|owners?|lines?|carriers?)\b'
    r'(?:[^?]{{0,20}}?\bcall(?:s|ing)?\s+(?:at\s+|in\s+|into\s+)?'
    r'(?P<account>[a-z][^?]*?)\s*\??\s*$)?')

PATTERNS = [
    # ── what to update today (before `my_day`'s "what … today") ──────
    (r'\bworkbench\b|'
     r'\b(what|which|anything)\b[^?]{0,40}\b(to |need|needs|needing|should|'
     r'must|have to)\b[^?]{0,30}\bupdat(e|ed|ing)\b|'
     r'\bupdat(e|es|ing) (do i|are) (need|needed|due|pending)\b|'
     r'\bwhat needs updating\b',
     'what_to_update_today', {}),

    # ── overdue quotations (before the general quote rules) ──────────
    (r'\b(overdue|late|lapsed|past.due)\s+(quotes?|quotations?)\b|'
     r'\b(quotes?|quotations?)\s+(that are |which are |are )?'
     r'(overdue|late|past due|past deadline|past their deadline|'
     r'past the deadline)\b|'
     r'\bquot(e|ation) deadlines? (missed|passed|breached)\b',
     'quotes_overdue', {}),

    # ── RFQ ageing by age, not by deadline ───────────────────────────
    (r'\brfqs?\b[^?]{0,40}\b(\d+\s*)?days?\s+old\b|'
     r'\brfqs?\s+older than\b|'
     r'\brfq (ageing|aging)\b|\b(ageing|aging) of rfqs?\b|'
     r'\bhow old are (the |our |my )?rfqs?\b',
     'rfqs_older_than', {}),

    # ── accounts I personally have not contacted ─────────────────────
    (r'\baccounts?\b[^?]{0,30}\bhave i not (contacted|spoken to|called|'
     r'touched|visited)\b|'
     r'\baccounts? i (have not|haven.t|did not|didn.t) (contacted|called|'
     r'spoken to|touched|visited)\b|'
     r'\bmy accounts?\b[^?]{0,30}\b(not contacted|no contact|gone quiet|'
     r'never contacted)\b|'
     r'\bwhich of my accounts\b[^?]{0,40}\bcontact',
     'accounts_not_contacted', {}),

    # ── who handles THIS account (before the general `who handles`) ──
    (r'\bwho (handles|owns|manages|looks after|is looking after|'
     r'is the pic for|is in charge of)\s+(this|the|that)\s+'
     r'(account|customer|client|company)\b|'
     r'\bwho (in (our|the) team|at procam|on our side) (handles|owns|'
     r'manages|looks after)\b|'
     r'\bwhich pic (handles|owns|has)\b',
     'who_handles_account', {}),

    # ── the weekly review ────────────────────────────────────────────
    (r'\b(weekly|week.?s|monday) (sales )?review\b|'
     r'\b(sales )?review (pack|pre[ap]\w*)\b|'
     r'\bprepare (my |the |our )?(weekly|sales) review\b|'
     r'\bmy week in review\b|\breview my week\b',
     'my_weekly_review', {}),

    # ── market intelligence ──────────────────────────────────────────
    (r'\bepc\b[^?]{0,40}\b(appointed|awarded|selected|named|contractor)\b|'
     r'\b(projects?)\b[^?]{0,40}\b(appointed|awarded|named|have|with|has)\s+'
     r'(an?\s+)?epc\b|'
     r'\bepc (appointments?|contractors?)\b',
     'projects_with_epc', {}),

    (r'\bcompetitors?\b[^?]{0,60}\b(executed|recently (did|won|handled|'
     r'moved|executed)|project work|projects?)\b|'
     r'\b(recent|latest) competitor (activity|projects?|wins?|moves?)\b|'
     r'\bwhat (have|are) (our )?competitors? (been )?(doing|winning|'
     r'executing)\b',
     'competitors_recent_projects', {}),

    # One rule per vessel type, because a fixed param is the only way
    # to carry "MPV" through `_match_rules`, which has no extractor for
    # anything but an account name. The port rides in on the `account`
    # group for the same reason — see the handler, and the one-line
    # `_capture: 'port'` alternative in the release notes.
    (_VESSEL_RX.format(kind=r'mpv|multi.?purpose'),
     'vessel_operators_calling', {'vessel_type': 'mpv', '_group': True}),
    (_VESSEL_RX.format(kind=r'breakbulk|break.bulk'),
     'vessel_operators_calling',
     {'vessel_type': 'breakbulk', '_group': True}),
    (_VESSEL_RX.format(kind=r'heavy.?lift'), 'vessel_operators_calling',
     {'vessel_type': 'heavy_lift', '_group': True}),
    (_VESSEL_RX.format(kind=r'ro.?ro'), 'vessel_operators_calling',
     {'vessel_type': 'roro', '_group': True}),
    (_VESSEL_RX.format(kind=r'vessel|ship|shipping|ocean'),
     'vessel_operators_calling', {'_group': True}),
    (r'\b(operators?|carriers?|lines?)\s+(that\s+|which\s+)?'
     r'call(?:s|ing)?\s+(?:at\s+|in\s+|into\s+)?(?P<account>[a-z][^?]*?)'
     r'\s*\??\s*$',
     'vessel_operators_calling', {'_group': True}),

    (r'\b(which|what|whose)\s+contacts?\b[^?]{0,40}\b(do|did)\s+we\s+know\b|'
     r'\bcontacts?\s+(that\s+|which\s+)?we\s+know\b|'
     r'\bwho\s+(do|did)\s+we\s+know\s+at\b|'
     r'\bknown contacts?\b|\bcontact directory\b',
     'contacts_at_company', {}),
]


#: Set once `install_patterns()` has run, so importing this module twice
#: (which the test suite does) cannot insert the rules twice.
_INSTALLED = False


def install_patterns(service=None):
    """Put `PATTERNS` at the front of the Copilot's rule list.

    Done in code rather than by editing `app/copilot/service.py` because
    Release 6 may not touch that file. It is the same list, in the same
    order, in the same place — the release notes carry the literal
    tuples so they can be moved into `_PATTERNS` whenever that file is
    next open, and `install_patterns()` becomes a no-op at that point
    because of the duplicate guard below.
    """
    global _INSTALLED
    if _INSTALLED:
        return False
    if service is None:
        from app.copilot import service as service_mod
        service = service_mod
    existing = {key for _rx, key, _spec in service._PATTERNS}
    fresh = [p for p in PATTERNS if p[1] not in existing]
    if fresh:
        service._PATTERNS[:0] = fresh
    _INSTALLED = True
    return bool(fresh)
