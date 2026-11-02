"""
The monthly RFQ target: reading the history, and proposing from it.

PROCAM-DECISIONS-02NOV Module 3. "The system pre-fills each person's
average monthly RFQ count over the last 4 months, plus 20%."

What counts as an RFQ
    Three possible answers, and they do not agree on this database:

      `rfqs`                     the RFQ Chain's own table
      `lead_stage_history`       leads that reached RFQ Generated
      `leads.rfq_date`           the date on the lead

    On production the first is effectively empty — the RFQ Chain is
    deployed and barely used — so a target read from it would be zero
    for everybody, and a zero target is not a target. So `counted()`
    tries all three in that order and **says which one it used**,
    because a number nobody can trace to a source is a number nobody
    will accept being measured against.

What happens when there is no history
    Nothing. `proposals()` returns the person under `no_history`
    rather than proposing zero. A target of 0 + 20% is 0, it would
    pass every review automatically, and it would look like a
    decision somebody made. An administrator setting it by hand is
    the honest outcome, and the screen asks for exactly that.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

#: Module 3.1 — four months, plus twenty per cent.
MONTHS = 4
UPLIFT = 1.20

#: The four selling verticals. Management and Other are where a
#: record goes when nothing else fits — they are not services
#: anybody raises an RFQ for, and `master_vertical()` falls back to
#: Management by name, so without this list every head-office
#: employee is proposed an RFQ target.
SELLING = ('Heavy Transport', 'Project Freight', 'Installation',
           'Warehousing')

#: Where the count came from, most trustworthy first.
FROM_RFQS = 'the rfqs table'
FROM_STAGE = 'leads that reached RFQ Generated'
FROM_RFQ_DATE = 'the rfq_date on the lead'
FROM_NOTHING = 'nothing — no RFQ history at all'


def month_start(when=None):
    """The first of the month, in IST.

    `date.today()` is the server's day, and on the last of the month
    between 18:30 and midnight UTC it is already the first in Mumbai
    — so a target seeded then would land in the month that had just
    ended.
    """
    from app.services import quote_due as qd

    today = when or qd.today_ist()
    return date(today.year, today.month, 1)


def month_key(when=None):
    """'2026-11' — the form `rfq_targets.month` has always held.

    The table predates this module (CRM-LEAD-JOURNEY-01 §6.3) and
    keeps the month as a string, on the reasoning that every query
    against it is an equality or a prefix. That is still true, so
    this module reads and writes the same string rather than
    changing a column three other readers use.
    """
    if isinstance(when, str) and len(when) == 7:
        return when
    start = when if isinstance(when, date) else month_start(when)
    return f'{start:%Y-%m}'


def window(months=MONTHS, when=None):
    """(start, end) — the last four whole months, ending yesterday.

    The current month is left out. A target seeded on the 3rd would
    otherwise average three days of work into a monthly figure and
    propose a target a tenth of the right size.
    """
    end = month_start(when)
    start = end
    for _ in range(months):
        start = date(start.year - (start.month == 1),
                     12 if start.month == 1 else start.month - 1, 1)
    return start, end


def counted(start, end):
    """({(emp_code, vertical): n}, which source) over the window."""
    from app import Lead, db

    from_rfqs = _from_rfqs(start, end)
    if from_rfqs:
        return from_rfqs, FROM_RFQS
    from_stage = _from_stage_history(start, end)
    if from_stage:
        return from_stage, FROM_STAGE
    from_date = _from_rfq_date(start, end)
    if from_date:
        return from_date, FROM_RFQ_DATE
    return {}, FROM_NOTHING


def _vertical(raw):
    """The selling vertical a record is in, or ''.

    One of the four is itself, without a lookup. `master_vertical()`
    is only asked about the rest — the aliases, Sea Freight and
    General Transport and the others — because it falls back to
    Management by name, and on a database whose vertical master has
    not been loaded that fallback turns *every* vertical into
    Management and quietly empties this whole module. A name that is
    already a master vertical should not need a table to stay
    itself.
    """
    name = (raw or '').strip()
    if not name:
        return ''
    for known in SELLING:
        if name.lower() == known.lower():
            return known
    from app.services import vertical_heads

    mapped = (vertical_heads.master_vertical(name) or '').strip()
    return mapped if mapped in SELLING else ''


def _from_rfqs(start, end):
    from app import Lead, db

    try:
        from app.models.rfq import RFQ
    except Exception:                                     # noqa: BLE001
        return {}
    out = {}
    try:
        rows = (RFQ.query
                .filter(RFQ.created_at >= start)
                .filter(RFQ.created_at < end).all())
    except Exception:                                     # noqa: BLE001
        db.session.rollback()
        return {}
    for row in rows:
        owner = ''
        lead = None
        if getattr(row, 'lead_id', None):
            lead = db.session.get(Lead, row.lead_id)
        owner = ((getattr(row, 'created_by_id', '') or '')
                 or (getattr(lead, 'assigned_to', '') or '')).strip()
        vertical = _vertical(getattr(lead, 'procam_vertical', None)
                             or getattr(row, 'vertical', None))
        if owner and vertical:
            out[(owner, vertical)] = out.get((owner, vertical), 0) + 1
    return out


def _from_stage_history(start, end):
    from app import Lead, db

    try:
        from app.models.journey import LeadStageHistory
    except Exception:                                     # noqa: BLE001
        return {}
    out = {}
    try:
        rows = (LeadStageHistory.query
                .filter(LeadStageHistory.to_stage == 'RFQ Generated')
                .filter(LeadStageHistory.changed_at >= start)
                .filter(LeadStageHistory.changed_at < end).all())
    except Exception:                                     # noqa: BLE001
        db.session.rollback()
        return {}
    for row in rows:
        lead = db.session.get(Lead, row.lead_id)
        if lead is None:
            continue
        # The person who moved it, not the owner today: a target is
        # about what somebody did.
        owner = ((getattr(row, 'changed_by', '') or '')
                 or (lead.assigned_to or '')).strip()
        vertical = _vertical(lead.procam_vertical)
        if owner and vertical:
            out[(owner, vertical)] = out.get((owner, vertical), 0) + 1
    return out


def _from_rfq_date(start, end):
    from app import Lead, db

    out = {}
    try:
        rows = (Lead.query
                .filter(Lead.rfq_date.isnot(None))
                .filter(Lead.rfq_date >= start)
                .filter(Lead.rfq_date < end).all())
    except Exception:                                     # noqa: BLE001
        db.session.rollback()
        return {}
    for lead in rows:
        owner = (lead.assigned_to or '').strip()
        vertical = _vertical(lead.procam_vertical)
        if owner and vertical:
            out[(owner, vertical)] = out.get((owner, vertical), 0) + 1
    return out


def proposals(*, months=MONTHS, when=None):
    """What the seed would write.

    Returns {'period', 'from', 'source', 'to', 'rows', 'no_history'}:

      rows        [{emp_code, name, vertical, counted, average,
                    target, basis}]
      no_history  [{emp_code, name, vertical}] — salespeople with
                  nothing to read, reported rather than given 0
    """
    from app import Employee

    start, end = window(months, when=when)
    tally, source = counted(start, end)
    period = month_start(when)

    rows, nothing = [], []
    seen = set()
    for (emp_code, vertical), count in sorted(tally.items()):
        if vertical not in SELLING:
            continue
        emp = Employee.query.filter_by(emp_code=emp_code).first()
        if emp is None or not emp.is_active:
            # A target for somebody who has left is a target nobody
            # is measured against, and it would show in every review
            # as a gap.
            continue
        average = count / float(months)
        # Rounded up. A target of 6.5 RFQs is not a thing somebody
        # can hit, and rounding down sets the bar below what they
        # already did.
        target = int(math.ceil(average * UPLIFT))
        rows.append({
            'emp_code': emp_code, 'name': emp.name or emp_code,
            'vertical': vertical, 'counted': count,
            'average': round(average, 2), 'target': target,
            'basis': (f'{count} in {months} months = '
                      f'{average:.2f}/month, +20% ({source})')[:200],
        })
        seen.add((emp_code, vertical))

    for emp in _salespeople():
        vertical = _vertical(emp.vertical)
        if not vertical or (emp.emp_code, vertical) in seen:
            continue
        nothing.append({'emp_code': emp.emp_code,
                        'name': emp.name or emp.emp_code,
                        'vertical': vertical})
    return {'period': period, 'from': start, 'to': end,
            'source': source, 'rows': rows, 'no_history': nothing}


def _salespeople():
    """Who gets an RFQ target at all.

    Everybody active in one of the four selling verticals.
    Deliberately not filtered by role: 'presales' and 'user' both
    raise RFQs, and a role-based list would leave out the people who
    raise most of them.
    """
    from app import Employee

    rows = (Employee.query
            .filter(Employee.is_active.is_(True))
            .order_by(Employee.name.asc()).all())
    return [e for e in rows if _vertical(e.vertical) in SELLING]


# ── reading and writing one target ──────────────────────────────────
def get(emp_code, vertical, period=None):
    from app.models.rfq_target import RfqTarget

    return RfqTarget.query.filter_by(
        emp_code=emp_code, vertical=vertical,
        month=month_key(period)).first()


def for_period(period=None, *, vertical=None):
    from app.models.rfq_target import RfqTarget

    query = RfqTarget.query.filter_by(month=month_key(period))
    if vertical:
        query = query.filter_by(vertical=vertical)
    return query.order_by(RfqTarget.vertical, RfqTarget.emp_code).all()


def history(emp_code=None, vertical=None, *, limit=200):
    from app.models.rfq_target import RfqTargetHistory

    query = RfqTargetHistory.query
    if emp_code:
        query = query.filter_by(emp_code=emp_code)
    if vertical:
        query = query.filter_by(vertical=vertical)
    return (query.order_by(RfqTargetHistory.created_at.desc())
            .limit(limit).all())


def set_target(emp_code, vertical, target, *, period=None,
               actor=None, reason='', source=None, basis=None):
    """Write a target and log the change. Returns the row.

    Every write goes in the history, including the first one. "Who
    set this, and what was it before" has to have an answer for the
    original value too, or the first edit looks like the only
    decision anybody made.
    """
    from app import db
    from app.models.rfq_target import (MANUAL, RfqTarget,
                                       RfqTargetHistory, SEEDED)
    from app.services import audit

    emp_code = (emp_code or '').strip()
    vertical = (vertical or '').strip()
    if not emp_code or not vertical:
        raise ValueError('A target belongs to one person and one '
                         'vertical; name both.')
    try:
        target = int(target)
    except (TypeError, ValueError):
        raise ValueError('A target is a whole number of RFQs.')
    if target < 0:
        raise ValueError('A target cannot be negative.')

    month = month_key(period)
    row = get(emp_code, vertical, month)
    was = row.target_rfqs if row is not None else None
    if row is None:
        row = RfqTarget(emp_code=emp_code, vertical=vertical,
                        month=month)
        db.session.add(row)
    row.target_rfqs = target
    row.source = source or (SEEDED if was is None else MANUAL)
    if basis is not None:
        row.basis = basis[:200]
    row.updated_by = actor
    db.session.flush()

    db.session.add(RfqTargetHistory(
        target_id=row.id, emp_code=emp_code, vertical=vertical,
        month=month, old_target=was, new_target=target,
        reason=(reason or '')[:300], actor=actor))
    audit.record('rfq_target.set', 'employee', emp_code,
                 old={'target_rfqs': was},
                 new={'target_rfqs': target, 'vertical': vertical,
                      'month': month},
                 actor=actor,
                 reason=(reason or f'RFQ target for {vertical}, '
                                   f'{month}'),
                 commit=False)
    return row
