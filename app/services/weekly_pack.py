"""
The Thursday review pack and the Friday freeze.

Two reports with one purpose: make the Monday sales review about
decisions rather than about whose number is right.

**Thursday, 14:00 IST — the review pack.** Sent before the meeting, to
the people who will be in it, so nobody spends the first twenty minutes
reading. It is the week's movement: what came in, what moved stage,
what was won and lost, what is sitting past its quote deadline, and the
accounts nobody has touched.

**Friday, 09:00 IST — the freeze.** The same numbers, written down.
`weekly_pipeline_snapshot` holds them, so the figure quoted in the
meeting is the figure that was true when the week closed and not the
one the screen shows while somebody is editing a lead. A review that
can be argued with by pressing refresh is not a review.

Everything is built on `digests` and `sales_rules`: the definitions of
open, overdue, high value and the ageing buckets are already settled
there, and a second opinion in this file would be a third set of
numbers for the meeting to argue about.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from app.services import digests, sales_rules as rules

#: The pack's own sections cap, a little wider than a digest's: this is
#: read at a desk before a meeting, not on a phone at 19:00.
MAX_ROWS = 25

LEASE_FREEZE = 'weekly_freeze'


def week_label(day=None):
    """ISO week, e.g. '2026-W40'. The key everything is filed under."""
    day = day or rules.business_today()
    iso = day.isocalendar()
    return f'{iso[0]}-W{iso[1]:02d}'


def _money(value):
    try:
        return Decimal(str(value or 0))
    except Exception:
        return Decimal('0')


# ── the numbers ──────────────────────────────────────────────────────
def _open_leads(sc):
    """Every open lead inside a scope — the pipeline, not the to-do list.

    This deliberately does not come from the Workbench board. The board
    answers "what needs doing", so a healthy lead that nobody has to
    touch this week is not on it; counting the board as the pipeline
    would report a well-run vertical as an empty one.
    """
    from app import Lead
    from app.access import scope as sc_mod

    query = Lead.query
    if hasattr(Lead, 'is_archived'):
        query = query.filter((Lead.is_archived.is_(False))
                             | (Lead.is_archived.is_(None)))
    try:
        query = sc_mod.leads(query, sc=sc)
    except Exception:
        return []
    return [l for l in query.all()
            if 'won' not in (l.stage or '').lower()
            and 'lost' not in (l.stage or '').lower()]


def _lead_value(lead):
    return _money(getattr(lead, 'quote_value_num', None)
                  or getattr(lead, 'opportunity_value_num', None)
                  or getattr(lead, 'estimated_value_inr', None))


def scope_figures(sc, today=None):
    """One scope's week: the pipeline, and what needs attention in it.

    Two sources on purpose, and the difference is the point. The
    pipeline is every open lead. The attention counts come from the
    same Workbench board the person sees when they click through, so a
    figure in the pack and a figure on the screen cannot disagree.
    """
    board = digests._board(sc, today=today)
    items = digests._all_items(sc, board, today=today)

    def _with(cond):
        return [i for i in items if cond in (i.get('conditions') or ())]

    open_leads = _open_leads(sc)
    figures = {
        'open_count': len(open_leads),
        'open_value': sum((_lead_value(l) for l in open_leads), Decimal('0')),
        'needs_attention': board.get('total', len(items)),
        'overdue_count': len(_with('followup_overdue')),
        'quotes_pending': (len(_with('quote_due_soon'))
                           + len(_with('quote_overdue'))),
        'high_value': len(_with('high_value')),
        'idle': len(_with('idle')),
        'stale': len(_with('stale')),
        'untouched_accounts': len(_with('account_quiet')),
        'total': board.get('total', len(items)),
    }
    return figures, items, board


def _won_lost(sc, since, until):
    """Closed this week, from the leads themselves.

    The board is about what needs doing, so it does not carry what is
    already finished; won and lost have to be counted separately.
    """
    from app import Lead
    from app.access import scope as sc_mod

    out = {'won_count': 0, 'won_value': Decimal('0'),
           'lost_count': 0, 'lost_value': Decimal('0')}
    try:
        query = sc_mod.leads(
            Lead.query.filter(Lead.updated_at >= since,
                              Lead.updated_at < until), sc=sc)
        for lead in query.all():
            stage = (lead.stage or '').lower()
            value = _money(getattr(lead, 'quote_value_num', None)
                           or getattr(lead, 'opportunity_value_num', None)
                           or getattr(lead, 'estimated_value_inr', None))
            if 'won' in stage:
                out['won_count'] += 1
                out['won_value'] += value
            elif 'lost' in stage:
                out['lost_count'] += 1
                out['lost_value'] += value
    except Exception:
        pass
    return out



def register_summary():
    """The client block register, for the Thursday pack.

    Read before the review rather than discovered during it: a block
    agreed on Tuesday changes what can be said about a client's
    pipeline on Monday.
    """
    try:
        from app.models.restriction import (BLOCKED, CAUTION,
                                            ClientRestriction, RECOMMENDED)
        rows = (ClientRestriction.query
                .filter(ClientRestriction.status.in_(
                    (BLOCKED, CAUTION, RECOMMENDED)))
                .order_by(ClientRestriction.status,
                          ClientRestriction.company_name).all())
    except Exception:
        return {'blocked': [], 'caution': [], 'pending': [], 'total': 0}

    def _row(r):
        return {'name': r.company_name, 'reason': r.reason_category or '',
                'since': str(r.approved_at or r.recommended_at)[:10]}

    out = {
        'blocked': [_row(r) for r in rows if r.status == BLOCKED],
        'caution': [_row(r) for r in rows if r.status == CAUTION],
        'pending': [_row(r) for r in rows if r.status == RECOMMENDED],
    }
    out['total'] = sum(len(v) for v in out.values() if isinstance(v, list))
    return out


# ── the Thursday pack ────────────────────────────────────────────────
def review_pack(emp_code, *, today=None):
    """What one person needs in front of them before the review."""
    today = today or rules.business_today()
    person = digests._person(emp_code)
    sc = digests._scope(emp_code)
    figures, items, board = scope_figures(sc, today=today)
    closed = _won_lost(sc, datetime.utcnow() - timedelta(days=7),
                       datetime.utcnow())
    figures.update(closed)

    # Built with the digest's own section builder, so a row in the pack
    # is laid out, linked and sorted exactly as the same row is in the
    # daily email. One shape, one macro, one thing to fix.
    sections = []
    for cond, title in (
            ('quote_overdue', 'Quotes past their deadline'),
            ('followup_overdue', 'Follow-ups already missed'),
            ('high_value', 'High-value deals'),
            ('idle', 'Nothing has happened for a week'),
            ('account_quiet', 'Accounts nobody has touched')):
        rows = [i for i in items if cond in (i.get('conditions') or ())]
        if rows:
            sections.append(digests._section(cond, rows, label=title,
                                             limit=MAX_ROWS))

    return {
        'report': 'weekly_pack',
        'template': 'email/weekly_pack.html',
        'restrictions': register_summary(),
        'date': str(today),
        'week': week_label(today),
        'person': person,
        'figures': figures,
        'sections': sections,
        'people': digests._by_owner(items)[:MAX_ROWS],
        'verticals': digests._by_vertical(items),
        'empty': not items,
        'link': '/team-workbench',
    }


# ── the Friday freeze ────────────────────────────────────────────────
def _scopes_to_freeze():
    """(scope_key, label, Scope) for the company, each vertical, each head.

    Per-person rows are not written: forty rows a week that nobody
    reads is a table that has to be maintained forever. The pack's
    per-person table is computed from the board when it is sent, and
    the frozen figures are the ones a meeting argues about.
    """
    from app import Employee
    from app.access import scope as sc_mod

    out = [('company', 'Procam', digests.company_scope())]
    seen = set()
    try:
        heads = (Employee.query
                 .filter(Employee.is_vertical_head.is_(True),
                         Employee.is_active.isnot(False)).all())
    except Exception:
        heads = []
    for head in heads:
        vertical = (head.vertical or '').strip()
        key = f'vertical:{vertical or head.emp_code}'
        if key in seen:
            continue
        seen.add(key)
        out.append((key, vertical or head.name or head.emp_code,
                    sc_mod.for_employee(head.emp_code)))
    return out


def freeze(*, today=None, commit=True):
    """Write the week down. Returns what was written.

    Idempotent on (week, scope): running it twice in a week updates the
    same rows rather than adding a second version of the truth.
    """
    import json

    from app import db
    from app.models.mailops import WeeklyPipelineSnapshot

    today = today or rules.business_today()
    label = week_label(today)
    now = datetime.utcnow()
    written = []

    for scope_key, scope_label, sc in _scopes_to_freeze():
        try:
            figures, _items, _board = scope_figures(sc, today=today)
            closed = _won_lost(sc, now - timedelta(days=7), now)
            figures.update(closed)

            row = (WeeklyPipelineSnapshot.query
                   .filter_by(week_label=label, scope_key=scope_key).first())
            if row is None:
                row = WeeklyPipelineSnapshot(week_label=label,
                                             scope_key=scope_key)
                db.session.add(row)
            row.scope_label = scope_label[:160]
            row.taken_at = now
            row.open_count = figures['open_count']
            row.open_value = figures['open_value']
            row.won_count = figures['won_count']
            row.won_value = figures['won_value']
            row.lost_count = figures['lost_count']
            row.quotes_pending = figures['quotes_pending']
            row.overdue_count = figures['overdue_count']
            row.payload_json = json.dumps(
                {k: (str(v) if isinstance(v, Decimal) else v)
                 for k, v in figures.items()})
            written.append({'scope': scope_key, 'label': scope_label,
                            **{k: str(v) for k, v in figures.items()}})
        except Exception:
            from flask import current_app
            current_app.logger.exception('freeze failed for %s', scope_key)
    if commit:
        db.session.commit()
    return {'week': label, 'taken_at': str(now)[:19], 'rows': written}


def frozen(week=None, scope_key=None):
    """Read a frozen week back."""
    from app.models.mailops import WeeklyPipelineSnapshot
    query = WeeklyPipelineSnapshot.query
    if week:
        query = query.filter_by(week_label=week)
    if scope_key:
        query = query.filter_by(scope_key=scope_key)
    return query.order_by(WeeklyPipelineSnapshot.week_label.desc(),
                          WeeklyPipelineSnapshot.scope_key).all()


def freeze_report(*, today=None, week=None):
    """The Friday email: the frozen figures, with last week beside them."""
    today = today or rules.business_today()
    label = week or week_label(today)
    rows = frozen(label)
    prior_day = today - timedelta(days=7)
    prior = {r.scope_key: r for r in frozen(week_label(prior_day))}

    table = []
    for row in rows:
        was = prior.get(row.scope_key)
        table.append({
            'scope': row.scope_key,
            'label': row.scope_label or row.scope_key,
            'open_count': row.open_count or 0,
            'open_value': row.open_value or 0,
            'won_count': row.won_count or 0,
            'won_value': row.won_value or 0,
            'lost_count': row.lost_count or 0,
            'quotes_pending': row.quotes_pending or 0,
            'overdue_count': row.overdue_count or 0,
            'delta_open': (row.open_count or 0) - ((was.open_count or 0)
                                                   if was else 0),
        })
    return {
        'report': 'weekly_freeze',
        'template': 'email/weekly_freeze.html',
        'date': str(today),
        'week': label,
        'taken_at': str(rows[0].taken_at)[:16] if rows else '',
        'rows': table,
        'empty': not table,
        'link': '/management',
    }
