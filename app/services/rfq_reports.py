"""
What the chain measures, read off its own timestamps.

CRM-RFQ-REPORTS-01 (R5), behind `FEATURE_RFQ_REPORTS`.

The SCM team's "200+ RFQs Analysis" sheet has a workload table
per person: RFQs by priority, on-time percentage, early count,
achieved against a 98% standard. Every figure on it is typed by
the people it measures, so nobody can check it and nobody quite
believes it.

Every figure here is computed from `started_at`, `completed_at`
and `paused_seconds`. Nothing is entered.

Two things the report refuses to do
    It does not count a step that has no standard time. Steps
    9-11 run on a response clock or the customer's own; scoring
    them as on-time or late would put the customer's delay in a
    Procam person's column.

    It does not report a percentage from one or two steps. A
    person with two closed steps is not at 50% on-time, they are
    unmeasured, and a league table that cannot tell those apart
    is one people learn to game.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta

FLAG = 'FEATURE_RFQ_REPORTS'

#: Below this many closed steps a person's on-time rate is not
#: reported. Small enough to include a real pilot, large enough
#: that one unlucky week is not a verdict.
MIN_FOR_RATE = 5


def enabled(emp_code=None):
    from flask import has_request_context, session

    from app.services import flags

    if emp_code is None and has_request_context():
        return flags.on_for(FLAG, session.get('emp_code'))
    if emp_code is None:
        return flags.on(FLAG)
    return flags.on_for(FLAG, emp_code)


def _visible_rfqs(sc=None):
    from app.models.rfq import RFQ
    try:
        from app.access import records
        return records.rfqs(sc=sc).all()
    except Exception:                                     # noqa: BLE001
        return RFQ.query.all()


def _chain_steps(rfqs):
    """[(rfq, task)] for every chain step on these RFQs."""
    from app.models.task_engine import TaskInstance

    if not rfqs:
        return []
    by_id = {r.id: r for r in rfqs}
    rows = (TaskInstance.query
            .filter(TaskInstance.entity_type == 'RFQ',
                    TaskInstance.entity_id.in_(list(by_id)),
                    TaskInstance.step_no.isnot(None)).all())
    return [(by_id[t.entity_id], t) for t in rows
            if t.entity_id in by_id]


# ── the tiles (§7) ───────────────────────────────────────────────────
def tiles(sc=None, *, month=None):
    """Every tile, and the filter each one opens."""
    from app.models.quote import Quote
    from app.models.task_engine import TaskInstanceStatus
    from app.services import rfq_chain as rc

    rfqs = _visible_rfqs(sc)
    pairs = _chain_steps(rfqs)
    live = defaultdict(list)
    for rfq, task in pairs:
        if task.status in TaskInstanceStatus.OPEN:
            live[rfq.id].append(task)

    received = len(rfqs)
    on_hold = sum(1 for r in rfqs if r.held_at)
    active = sum(1 for r in rfqs
                 if live.get(r.id) and not r.held_at)
    won = sum(1 for r in rfqs if (r.status or '') == 'Won')
    lost = sum(1 for r in rfqs if (r.status or '') == 'Lost')
    quoted = sum(1 for r in rfqs
                 if (r.status or '') in ('Quoted', 'Negotiation'))

    overdue = due_today = 0
    now = datetime.utcnow()
    for rfq, task in pairs:
        if task.status not in TaskInstanceStatus.OPEN or rfq.held_at:
            continue
        colour, _words = rc.verdict(task, priority=rfq.priority,
                                    now=now)
        if colour == rc.RED:
            overdue += 1
        elif colour == rc.ORANGE:
            due_today += 1

    closed = won + lost
    return {
        'received': received, 'active': active, 'on_hold': on_hold,
        'quoted': quoted, 'won': won, 'lost': lost,
        'overdue': overdue, 'due_today': due_today,
        'avg_response_hours': avg_response(rfqs),
        'prospective_value': _prospective(rfqs),
        'won_value': _won_value(rfqs),
        'conversion': (round(won * 100 / closed) if closed else None),
    }


def avg_response(rfqs):
    """RFQ received to quote sent, held time excluded.

    None when nothing has got that far — zero would read as
    instant.
    """
    from app.models.task_engine import TaskInstanceStatus
    from app.services import rfq_chain as rc

    spans = []
    for rfq, task in _chain_steps(rfqs):
        if task.step_no != 8 or task.status in TaskInstanceStatus.OPEN:
            continue
        first = min((t.started_at for _r, t in _chain_steps([rfq])
                     if t.started_at), default=None)
        if first is None or task.completed_at is None:
            continue
        held = sum(int(t.paused_seconds or 0)
                   for _r, t in _chain_steps([rfq]))
        hours = ((task.completed_at - first).total_seconds()
                 - held) / 3600.0
        if hours >= 0:
            spans.append(hours)
    if not spans:
        return None
    return round(sum(spans) / len(spans), 1)


def _prospective(rfqs):
    """Open quotes, latest revision only, in INR."""
    from app.models.quote import Quote

    ids = [r.id for r in rfqs]
    if not ids:
        return 0.0
    total, seen = 0.0, {}
    for quote in Quote.query.filter(Quote.rfq_id.in_(ids)).all():
        if (quote.status or '') in ('Superseded', 'Won', 'Lost',
                                    'Rejected'):
            continue
        root = quote.root_quote_id or quote.id
        rev = quote.rev_no if quote.rev_no is not None else 0
        if root not in seen or rev > seen[root][0]:
            seen[root] = (rev, float(quote.total_amount or 0))
    for _rev, amount in seen.values():
        total += amount
    return round(total, 2)


def _won_value(rfqs):
    from app.models.quote import Quote

    ids = [r.id for r in rfqs]
    if not ids:
        return 0.0
    return round(sum(
        float(q.total_amount or 0) for q in
        Quote.query.filter(Quote.rfq_id.in_(ids),
                           Quote.status == 'Won').all()), 2)


# ── the slowest step, and the workload table ─────────────────────────
def per_step(sc=None):
    """Average time against standard, per step. The bottleneck."""
    from app.models.task_engine import TaskInstanceStatus
    from app.services import rfq_chain as rc

    rows = defaultdict(list)
    for rfq, task in _chain_steps(_visible_rfqs(sc)):
        if task.status in TaskInstanceStatus.OPEN:
            continue
        standard = rc.standard_hours(task.step_no, rfq.priority)
        if not standard:
            continue          # no standard time: not scoreable
        rows[task.step_no].append(
            (rc.elapsed_hours(task), standard, task.result))

    out = []
    for step in rc.STEPS:
        got = rows.get(step['no'], [])
        if not got:
            out.append({'step_no': step['no'], 'title': step['title'],
                        'closed': 0, 'avg_hours': None,
                        'avg_standard': None, 'ratio': None,
                        'on_time_pct': None})
            continue
        spent = sum(g[0] for g in got) / len(got)
        standard = sum(g[1] for g in got) / len(got)
        ok = sum(1 for g in got if g[2] != rc.DELAYED)
        out.append({
            'step_no': step['no'], 'title': step['title'],
            'closed': len(got), 'avg_hours': round(spent, 1),
            'avg_standard': round(standard, 1),
            'ratio': round(spent / standard, 2) if standard else None,
            'on_time_pct': round(ok * 100 / len(got)),
        })
    return out


def slowest_step(sc=None):
    """The step furthest past its standard, or None."""
    scored = [r for r in per_step(sc)
              if r['ratio'] is not None and r['closed'] >= 3]
    if not scored:
        return None
    worst = max(scored, key=lambda r: r['ratio'])
    return worst if worst['ratio'] > 1 else None


def workload(sc=None):
    """The SCM sheet's table, computed instead of typed."""
    from app.models.task_engine import TaskInstanceStatus
    from app.services import rfq_chain as rc

    people = defaultdict(lambda: {
        'closed': 0, 'on_time': 0, 'early': 0, 'delayed': 0,
        'open': 0, 'late_open': 0,
        'by_priority': Counter()})

    now = datetime.utcnow()
    for rfq, task in _chain_steps(_visible_rfqs(sc)):
        who = task.owner_user_id or task.completed_by_id
        if not who:
            continue
        row = people[who]
        row['by_priority'][rfq.priority or 'unset'] += 1
        if task.status in TaskInstanceStatus.OPEN:
            row['open'] += 1
            if rc.verdict(task, priority=rfq.priority,
                          now=now)[0] == rc.RED:
                row['late_open'] += 1
            continue
        if not rc.standard_hours(task.step_no, rfq.priority):
            continue          # unscoreable: counted nowhere
        row['closed'] += 1
        if task.result == rc.EARLY:
            row['early'] += 1
            row['on_time'] += 1
        elif task.result == rc.ON_TIME:
            row['on_time'] += 1
        elif task.result == rc.DELAYED:
            row['delayed'] += 1

    target = rc.ON_TIME_TARGET
    out = []
    for code, row in people.items():
        closed = row['closed']
        pct = (round(row['on_time'] * 100 / closed)
               if closed >= MIN_FOR_RATE else None)
        out.append({
            'emp_code': code, 'closed': closed,
            'on_time': row['on_time'], 'early': row['early'],
            'delayed': row['delayed'], 'open': row['open'],
            'late_open': row['late_open'],
            'by_priority': dict(row['by_priority']),
            'on_time_pct': pct,
            # "Achieved" against the 98% standard, and None where
            # there are too few steps to say anything at all.
            'verdict': (None if pct is None else
                        ('Achieved' if pct >= target * 100
                         else 'Below target')),
            'gap': (None if pct is None
                    else round(target * 100 - pct)),
            'measured': closed >= MIN_FOR_RATE,
        })
    return sorted(out, key=lambda r: (-r['closed'], r['emp_code']))


def heatmap(sc=None):
    """Person × step, average time ÷ standard. None where unmeasured."""
    from app.models.task_engine import TaskInstanceStatus
    from app.services import rfq_chain as rc

    cells = defaultdict(list)
    for rfq, task in _chain_steps(_visible_rfqs(sc)):
        if task.status in TaskInstanceStatus.OPEN:
            continue
        standard = rc.standard_hours(task.step_no, rfq.priority)
        who = task.owner_user_id or task.completed_by_id
        if not standard or not who:
            continue
        cells[(who, task.step_no)].append(
            rc.elapsed_hours(task) / standard)

    people = sorted({k[0] for k in cells})
    return {
        'people': people,
        'steps': [{'step_no': s['no'], 'title': s['title']}
                  for s in rc.STEPS],
        'cells': [{'emp_code': who, 'step_no': step,
                   'ratio': round(sum(v) / len(v), 2),
                   'n': len(v)}
                  for (who, step), v in sorted(cells.items())],
    }


def reasons(sc=None):
    """Delay and good-practice reasons, by every cut §7 asks for."""
    from app.services import rfq_chain as rc

    delay = defaultdict(Counter)
    good = Counter()
    for rfq, task in _chain_steps(_visible_rfqs(sc)):
        if task.good_practice_reason:
            good[task.good_practice_reason] += 1
        if not task.delay_reason:
            continue
        who = task.owner_user_id or task.completed_by_id or '(nobody)'
        when = (task.completed_at or task.created_at)
        delay['reason'][task.delay_reason] += 1
        delay['step'][f'{task.step_no}. '
                      f'{rc.BY_NO.get(task.step_no, {}).get("title", "")}'] += 1
        delay['person'][who] += 1
        delay['vertical'][_vertical(rfq) or '(none)'] += 1
        delay['customer'][_customer(rfq) or '(none)'] += 1
        if when:
            delay['month'][f'{when.year:04d}-{when.month:02d}'] += 1
    return {'delay': {k: dict(v.most_common())
                      for k, v in delay.items()},
            'good_practice': dict(good.most_common())}


def _vertical(rfq):
    from app.services.vertical_scope import vertical_name
    return vertical_name(rfq)


def _customer(rfq):
    from app import Company, db
    if not rfq.account_id:
        return ''
    account = db.session.get(Company, rfq.account_id)
    return getattr(account, 'name', '') or ''


# ── §6.4: the time-per-step chart for one RFQ ────────────────────────
def time_chart(rfq):
    """One bar per step, from its start to its close or now."""
    from app.models.task_engine import TaskInstanceStatus
    from app.services import rfq_chain as rc

    live = rc.steps_for(rfq)
    bars, floor = [], None
    for step in rc.applicable(rfq):
        task = live.get(step['no'])
        if task is None or not task.started_at:
            bars.append({'step_no': step['no'],
                         'title': step['title'], 'start': None,
                         'hours': None, 'standard': None,
                         'result': '', 'colour': rc.GREY})
            continue
        if floor is None or task.started_at < floor:
            floor = task.started_at
        spent = rc.elapsed_hours(task)
        standard = rc.standard_hours(step['no'], rfq.priority)
        open_now = task.status in TaskInstanceStatus.OPEN
        colour = (rc.verdict(task, priority=rfq.priority)[0]
                  if open_now else
                  {rc.DELAYED: rc.RED, rc.EARLY: rc.GREEN,
                   rc.ON_TIME: rc.GREEN}.get(task.result, rc.GREY))
        bars.append({
            'step_no': step['no'], 'title': step['title'],
            'start': task.started_at.isoformat(timespec='minutes'),
            'hours': round(spent, 1),
            'standard': round(standard, 1) if standard else None,
            'held_hours': round(int(task.paused_seconds or 0) / 3600, 1),
            'result': task.result or ('open' if open_now else ''),
            'colour': colour, 'owner': task.owner_user_id or '',
        })
    return {'bars': bars,
            'from': floor.isoformat(timespec='minutes')
            if floor else None}


# ── §7: sales spend, which the TMS will answer ──────────────────────
def spend(sc=None):
    """Spend by lead, vertical and channel — once the TMS link exists.

    D7 put the IOU in the TMS and said the CRM shows the spend
    read-only. That link is explicitly out of this build, so there
    is nothing to show and nothing to compute. Returning an empty
    panel with the reason beats returning zeros: zero spend on a
    lead somebody has been working for a month is a figure a head
    would act on, and it would be false.
    """
    return {
        'available': False,
        'why': ('Spend comes from the TMS, and the CRM–TMS link is '
                'not built yet. IOUs for a lead are raised and seen '
                'there; this panel fills in once the link exists.'),
        'rows': [],
    }


# ── §7: what the Thursday pack adds ─────────────────────────────────
def weekly_additions(sc=None, *, limit=5):
    """The bottleneck step, the workload table and the top delays.

    Returns None when the chain has produced nothing yet, so the
    pack leaves the section out rather than printing three empty
    tables that make the chain look broken.
    """
    rows = workload(sc)
    slowest = slowest_step(sc)
    why = reasons(sc)
    top = list((why.get('delay') or {}).get('reason', {}).items())

    measured = [r for r in rows if r['measured']]
    if not rows and slowest is None and not top:
        return None
    return {
        'slowest': slowest,
        'workload': rows[:20],
        'measured': len(measured),
        'unmeasured': len(rows) - len(measured),
        'top_delays': top[:limit],
        # The chain owns this number; a second copy here would
        # drift the day somebody changes the setting.
        'target_pct': round(_target_pct()),
    }


def _target_pct():
    from app.services import rfq_chain as rc
    return rc.ON_TIME_TARGET * 100
