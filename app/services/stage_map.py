"""
One line that says where a lead is and whether it is stuck.

CRM 2.0 R2.

Why a line and not a stage name
    `lead.stage` says "RFQ Generated". It does not say the lead has
    been sitting on RFQ Generated for 41 days, which is the only part
    anybody acts on. The stage map puts the whole journey on one row:
    the stages behind it, the stage it is on, how long it has been
    there, and whether that is normal.

Where the timing comes from
    `stage_entered_at`, which is null on most rows because it was
    added after the fact — `scripts/crm2_backfill_stage_entered.py`
    fills it from the audit trail. Where it is still null the map says
    so rather than guessing: a grey stage is "we do not know when this
    started", which is different from "this started today".

    That distinction matters. `updated_at` is the obvious substitute
    and it is wrong: a bulk edit touched 2,381 leads on one day, so
    using it would have reported almost the whole book as freshly
    moved.

The colours
    Green within the target, orange past it, red past twice it. The
    targets below are a starting point, not a measurement — nothing in
    this database records how long a stage *should* take, because
    until the value gate went in last week most leads carried no
    figures at all. They are set per stage rather than one number for
    everything because a quote waiting a week is normal and a new lead
    waiting a week is not.
"""
from __future__ import annotations

from datetime import date, datetime

#: The journey, in order. Terminal states are not stages on the line;
#: they are where the line stops.
JOURNEY = (
    'New',
    'Call Done',
    'Business Discussion',
    'New Opportunity',
    'RFQ Generated',
    'Quoted',
    'Under Negotiation',
    'Won',
)

#: Stages that existed before this order was settled and that nobody
#: should be left stranded on. They sit at the same point on the line
#: as the stage they map to, so a lead on "Profile Sent" still draws.
ALIASES = {
    'Profile Sent': 'Call Done',
    'Appointment': 'Call Done',
    'Visit Done': 'Business Discussion',
}

TERMINAL = ('Won', 'Lost', 'On Hold', 'Not Interested')

#: Working days a stage is expected to take. Orange past it, red past
#: twice it. Deliberately not in Master Data yet: a number an
#: administrator can change is a number somebody will change before
#: anyone has measured what it really is.
TARGET_DAYS = {
    'New': 2,
    'Call Done': 5,
    'Business Discussion': 10,
    'New Opportunity': 10,
    'RFQ Generated': 7,
    'Quoted': 14,
    'Under Negotiation': 21,
}
DEFAULT_TARGET = 14

GREEN, ORANGE, RED, GREY = 'green', 'orange', 'red', 'grey'


def position(stage):
    """Where this stage sits on the line, or None if it is not on it."""
    stage = (stage or '').strip()
    stage = ALIASES.get(stage, stage)
    try:
        return JOURNEY.index(stage)
    except ValueError:
        return None


def target_for(stage):
    return TARGET_DAYS.get(ALIASES.get(stage, stage), DEFAULT_TARGET)


def verdict(stage, days):
    """green / orange / red, or grey when the age is unknown."""
    if days is None:
        return GREY
    target = target_for(stage)
    if days > target * 2:
        return RED
    if days > target:
        return ORANGE
    return GREEN


def age_days(since, *, today=None):
    """Whole days on this stage, counted in business days.

    `sales_rules.business_date` rather than `.date()`: the
    timestamps in this database are naive UTC and the business is
    at UTC+5:30, so taking the plain date off one and comparing it
    with a local today is the half-fix that module already
    documents. Between 18:30 and midnight UTC a lead moved an hour
    ago read as a day old — which on a map whose whole job is to
    say how long something has sat is a day of invented delay.
    """
    from app.services import sales_rules as rules

    if since is None:
        return None
    today = today or rules.business_today()
    return max(0, (rules.business_date(today)
                   - rules.business_date(since)).days)


def for_lead(lead, *, today=None):
    """The whole line for one lead.

    Returns the stages in order, each marked done / current / ahead,
    with the age and colour on the current one. A terminal lead draws
    its whole line as done and says how it ended, because "where did
    this go" is asked about closed leads more than open ones.
    """
    from app.services import sales_rules as rules

    today = today or rules.business_today()
    stage = (lead.stage or '').strip()
    closed = stage in TERMINAL and stage != 'Won'
    here = position(stage)

    days = age_days(getattr(lead, 'stage_entered_at', None), today=today)
    colour = GREY if closed else verdict(stage, days)

    steps = []
    for i, name in enumerate(JOURNEY):
        if here is None:
            state = 'unknown'
        elif i < here:
            state = 'done'
        elif i == here:
            state = 'current'
        else:
            state = 'ahead'
        steps.append({'stage': name, 'state': state,
                      'colour': colour if state == 'current' else ''})

    return {
        'lead_id': lead.id,
        'stage': stage,
        'on_the_line': here is not None,
        'closed': closed,
        'won': stage == 'Won',
        'days_in_stage': days,
        'target_days': target_for(stage),
        'colour': colour,
        'known': days is not None,
        'why': _why(stage, days, closed),
        'steps': steps,
    }


def _why(stage, days, closed):
    if closed:
        return f'closed as {stage}'
    if days is None:
        return ('no stage date on this lead yet — run the backfill to '
                'see how long it has been here')
    target = target_for(stage)
    if days > target * 2:
        return (f'{days} days on {stage}, against a target of '
                f'{target} — more than twice as long')
    if days > target:
        return f'{days} days on {stage}, against a target of {target}'
    return f'{days} days on {stage}, within the {target}-day target'


def pipeline(leads, *, today=None):
    """The same judgement across a book of leads, for the admin map.

    One pass, so a thousand leads is one walk rather than a query per
    stage. Terminal leads are left out: a map of where work is stuck
    has no room for the 8,340 that already stopped.
    """
    from app.services import sales_rules as rules

    today = today or rules.business_today()
    rows = {name: {'stage': name, 'total': 0, GREEN: 0, ORANGE: 0,
                   RED: 0, GREY: 0, 'oldest': 0, 'value': 0}
            for name in JOURNEY}
    for lead in leads:
        stage = (lead.stage or '').strip()
        if stage in TERMINAL:
            continue
        name = ALIASES.get(stage, stage)
        row = rows.get(name)
        if row is None:
            continue
        days = age_days(getattr(lead, 'stage_entered_at', None),
                        today=today)
        row['total'] += 1
        row[verdict(stage, days)] += 1
        if days and days > row['oldest']:
            row['oldest'] = days
        try:
            row['value'] += float(lead.estimated_value_inr or 0)
        except (TypeError, ValueError):
            pass
    return [rows[name] for name in JOURNEY if name != 'Won']
