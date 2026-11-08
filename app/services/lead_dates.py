"""When each thing on a lead actually happened, and how we know.

CRM-FINAL-CLOSEOUT-01 §5: "Auto-dates on every lead: real date →
audit-trail date → estimated, labelled which."

THE PROBLEM THIS SOLVES

Six date columns on `leads` are filled by hand and most of them are
empty. A lead sitting on Quoted with no `rfq_date` and no
`phone_call_date` has obviously been called and obviously received an
RFQ — it could not have reached Quoted otherwise — but every report
that counts "days from first contact to quote" skips it, so the
figures are computed from the minority of rows somebody typed dates
into. That is a worse number than no number, because it looks like a
measurement.

THE LADDER, AND WHY THE LABEL IS NOT OPTIONAL

Four rungs, best first. Every answer carries which rung it came from,
and nothing in this module ever returns a date without one:

    entered     the column on the lead. A person typed it, or the
                ingest wrote it from the mail. Treated as fact.

    audit       the lead's own record says when the event happened: a
                logged activity of the matching kind, a stage-history
                row entering the matching stage, or a `lead.stage_*`
                audit event. This dates the event itself.

    estimated   nothing records the event, but the lead is *past* the
                stage that requires it, so it must have happened on or
                before the earliest later step that is recorded. The
                date is that bound — a real timestamp out of this
                lead's own history, read as "no later than this".

    unknown     nothing to go on. Says so. Never a date.

An estimate is always a bound taken from a timestamp on *this* lead.
It is never an average, a typical gap, or a number derived from how
long these things usually take. That line is deliberate and it is the
same line `stage_map` already draws — "a grey stage is 'we do not
know when this started', which is different from 'this started
today'". A date invented from a company-wide average would be
indistinguishable, on the screen and in a CSV, from one somebody
typed; a bound read off a later step is checkable by the person
looking at it.

WHAT MUST NOT READ AN ESTIMATE

`estimated` is for reading, counting and sorting. It is not for
anything that computes a deadline, an SLA, an ageing colour or an
escalation: those decide whether somebody is late, and being told you
are late on an inferred date is the one outcome that makes people
stop trusting the whole screen. `firm_only()` exists for those
callers and returns the first two rungs alone.
"""
from __future__ import annotations

from datetime import date, datetime

#: The four rungs, best first.
ENTERED = 'entered'
AUDIT = 'audit'
ESTIMATED = 'estimated'
UNKNOWN = 'unknown'

#: What each rung is called on the screen. Short, because it sits in
#: a chip beside the date and a sentence there would wrap.
SOURCE_LABELS = {
    ENTERED: 'recorded',
    AUDIT: 'from the trail',
    ESTIMATED: 'estimated',
    UNKNOWN: 'not known',
}

#: The rungs a deadline may be computed from. See the docstring.
FIRM = (ENTERED, AUDIT)

#: Audit actions that carry a stage change. `lead.stage_moved` is
#: `lead_flow`'s; the other two are the audit listener's, and
#: `scripts/crm2_backfill_stage_entered.py` reads the same pair —
#: old rows have only these.
STAGE_ACTIONS = ('lead.stage_change', 'lead.stage_changed',
                 'lead.stage_moved')


#: One entry per date the CRM claims to know, in the order they
#: happen. `column` is the lead's own field; `kinds` are the
#: `lead_activities.kind` values that evidence it; `stage` is the
#: journey stage reaching which implies it happened.
#:
#: No employee names or codes here, and no stage spelling that
#: `stage_map` does not own — the stage strings are looked up through
#: `stage_map.canonical`, so a vertical using a retired name still
#: resolves.
MILESTONES = (
    {'key': 'created', 'label': 'Enquiry received',
     'column': 'created_at', 'kinds': (), 'stage': 'New',
     'what': 'when the enquiry reached the CRM'},
    {'key': 'first_call', 'label': 'First call',
     'column': 'phone_call_date', 'kinds': ('call',),
     'stage': 'Call Done',
     'what': 'when somebody first spoke to the customer'},
    {'key': 'intro_mail', 'label': 'Introduction sent',
     'column': 'intro_mail_date', 'kinds': ('email',),
     'stage': 'Call Done',
     'what': 'when Procam first wrote to them'},
    {'key': 'meeting', 'label': 'Meeting or visit',
     'column': 'meeting_date', 'kinds': ('meeting', 'visit'),
     'stage': 'Business Discussion',
     'what': 'when they were met'},
    {'key': 'rfq', 'label': 'RFQ received',
     'column': 'rfq_date', 'kinds': ('rfq',),
     'stage': 'RFQ Generated',
     'what': 'when the enquiry became a request to quote'},
    {'key': 'quote', 'label': 'Quotation sent',
     'column': 'quote_date', 'kinds': ('quote',),
     'stage': 'Quoted',
     'what': 'when a price went to the customer'},
)

BY_KEY = {m['key']: m for m in MILESTONES}


# ── small helpers ────────────────────────────────────────────────────
def _as_date(value):
    """A date from a date, a datetime, or nothing."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _answer(milestone, when, source, detail, *, bound=False):
    return {
        'key': milestone['key'],
        'label': milestone['label'],
        'what': milestone['what'],
        'date': _as_date(when),
        'source': source,
        'source_label': SOURCE_LABELS[source],
        'detail': detail,
        #: True when the date means "on or before this", not "on this".
        'bound': bool(bound),
        'firm': source in FIRM,
    }


def _journey_index(stage):
    """Where a stage sits on the journey, or None if it is not on it."""
    from app.services import stage_map

    canonical = stage_map.canonical(stage)
    if canonical in stage_map.JOURNEY:
        return stage_map.JOURNEY.index(canonical)
    return None


# ── the evidence, read once per lead ─────────────────────────────────
def evidence(lead):
    """Everything in this lead's record that can date something.

    Read in one pass and handed to every milestone, because a lead
    screen asks for six dates at once and six separate scans of the
    same three tables is the same answer at six times the cost.
    """
    from app import LeadActivity, LeadStageHistory
    from app.models.audit import AuditEvent
    from app.services import stage_map

    out = {'by_kind': {}, 'by_stage': {}, 'steps': []}
    if lead is None or not getattr(lead, 'id', None):
        return out

    # Activities: the earliest of each kind. The earliest, because
    # "first call" means the first one.
    try:
        rows = (LeadActivity.query
                .filter_by(lead_id=lead.id)
                .order_by(LeadActivity.occurred_at.asc())
                .all())
    except Exception:                                     # noqa: BLE001
        rows = []
    for row in rows:
        kind = (row.kind or '').strip().lower()
        when = _as_date(row.occurred_at)
        if not kind or when is None:
            continue
        out['by_kind'].setdefault(kind, when)
        out['steps'].append((when, f'a {kind} was logged'))

    # Stage history: when each stage was entered, the first time.
    try:
        hist = (LeadStageHistory.query
                .filter_by(lead_id=lead.id)
                .order_by(LeadStageHistory.changed_at.asc())
                .all())
    except Exception:                                     # noqa: BLE001
        hist = []
    for row in hist:
        stage = stage_map.canonical(row.to_stage)
        when = _as_date(row.changed_at)
        if not stage or when is None:
            continue
        out['by_stage'].setdefault(stage, (when, 'the stage history'))
        out['steps'].append((when, f'the lead moved to {stage}'))

    # Audit events, for the stages that predate the history table.
    # Only consulted where the history has nothing: two records of
    # one move would otherwise disagree by a day and the screen would
    # show whichever was read second.
    try:
        events = (AuditEvent.query
                  .filter(AuditEvent.entity_type == 'lead',
                          AuditEvent.entity_id == str(lead.id),
                          AuditEvent.action.in_(STAGE_ACTIONS))
                  .order_by(AuditEvent.occurred_at.asc())
                  .all())
    except Exception:                                     # noqa: BLE001
        events = []
    for ev in events:
        payload = ev.new_value if isinstance(ev.new_value, dict) else {}
        stage = stage_map.canonical(payload.get('stage'))
        when = _as_date(ev.occurred_at)
        if not stage or when is None:
            continue
        out['by_stage'].setdefault(stage, (when, 'the audit trail'))
        out['steps'].append((when, f'the lead moved to {stage}'))

    out['steps'].sort(key=lambda s: s[0])
    return out


# ── one milestone ────────────────────────────────────────────────────
def resolve_one(lead, milestone, ev=None):
    """The best answer for one milestone, with its rung."""
    ev = ev if ev is not None else evidence(lead)

    # 1 — the column.
    column = milestone.get('column')
    typed = _as_date(getattr(lead, column, None)) if column else None
    if typed is not None:
        return _answer(milestone, typed, ENTERED,
                       f'from the lead\'s own {column.replace("_", " ")}')

    # 2 — the trail. An activity dates the event; a stage move dates
    # the step that follows from it. The activity is preferred for
    # exactly that reason.
    for kind in milestone.get('kinds') or ():
        when = ev['by_kind'].get(kind)
        if when is not None:
            return _answer(milestone, when, AUDIT,
                           f'a {kind} logged on this lead')

    stage = milestone.get('stage')
    if stage:
        from app.services import stage_map
        canonical = stage_map.canonical(stage)
        found = ev['by_stage'].get(canonical)
        if found is not None:
            when, where = found
            return _answer(milestone, when, AUDIT,
                           f'the lead reached {canonical}, per {where}')

    # 3 — the bound. Nothing records the event, but the lead is past
    # the stage that requires it, so it happened no later than the
    # earliest *later* step on record.
    #
    # "Later" is the whole correctness of this rung. The earliest
    # dated thing on a lead is usually the first call, and reading
    # that as the bound for the RFQ would date the RFQ before the
    # call — a lower bound printed as an upper one. So only steps
    # that sit after this milestone on the journey count, and the
    # earliest of those is the answer.
    index = _journey_index(stage) if stage else None
    if index is not None:
        from app.services import stage_map

        after = []
        for seen, (when, where) in ev['by_stage'].items():
            seen_index = (stage_map.JOURNEY.index(seen)
                          if seen in stage_map.JOURNEY else None)
            if seen_index is not None and seen_index > index:
                after.append((when, f'the lead reached {seen}', where))
        for other in MILESTONES:
            other_index = _journey_index(other.get('stage'))
            if other_index is None or other_index <= index:
                continue
            for kind in other.get('kinds') or ():
                when = ev['by_kind'].get(kind)
                if when is not None:
                    after.append((when, f'a {kind} was logged',
                                  'the activity log'))

        # The lead's own stage counts as having been reached even
        # where nothing dated the move, because the stage it is on is
        # not in doubt — but it dates nothing by itself, so it only
        # decides whether this milestone happened, not when.
        reached = _journey_index(getattr(lead, 'stage', None))
        passed = (reached is not None and reached > index) or bool(after)

        if passed and after:
            after.sort(key=lambda row: row[0])
            when, what, _where = after[0]
            return _answer(
                milestone, when, ESTIMATED,
                f'nothing records it, but {what} on this date — so '
                f'this happened on or before then',
                bound=True)
        if passed:
            return _answer(
                milestone, None, UNKNOWN,
                f'the lead got past {milestone["stage"]}, so this did '
                f'happen — but nothing later in its record carries a '
                f'date, so there is nothing to bound it with')

    # 4 — nothing.
    return _answer(milestone, None, UNKNOWN,
                   'nothing on the lead or in its trail dates this')


def stage_name(lead):
    from app.services import stage_map
    return stage_map.canonical(getattr(lead, 'stage', None)) or 'nowhere'


# ── the whole lead ───────────────────────────────────────────────────
def for_lead(lead):
    """Every milestone on one lead, in the order they happen."""
    ev = evidence(lead)
    return [resolve_one(lead, m, ev) for m in MILESTONES]


def firm_only(lead):
    """The milestones a deadline may be computed from.

    See the module docstring: an estimate is a bound for a reader,
    not an input to anything that decides whether somebody is late.
    """
    return [row for row in for_lead(lead) if row['firm']]


def summary(lead):
    """How much of this lead's timeline is known, and how."""
    rows = for_lead(lead)
    counts = {ENTERED: 0, AUDIT: 0, ESTIMATED: 0, UNKNOWN: 0}
    for row in rows:
        counts[row['source']] += 1
    return {
        'dates': rows,
        'counts': counts,
        'known': len(rows) - counts[UNKNOWN],
        'total': len(rows),
        'stage': stage_name(lead),
    }
