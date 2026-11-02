"""
The next follow-up date: proposed by the CRM, accepted by the owner.

CRM-COCKPIT-02 §3. Four of the hundred most recent leads had a
follow-up date, so the overdue alerts — which are the whole of the
daily brief, the My Work strip and half the cockpit — were reading a
field almost nobody filled. Asking people to fill it harder has been
tried. This proposes the date instead, one tap to accept.

The rule, per stage, from Admin → CRM rules:

    New / New Opportunity   +1 day
    Contacted, Call done,
    Business discussion     +3 days
    RFQ Generated           one day before the quote due date
    Quoted                  +2 days
    Under Negotiation       +2 days

RFQ Generated is the one that is not an interval, and it is the reason
this module exists rather than a dictionary lookup at each call site.
At that stage the only date that matters is the submission deadline,
so the follow-up sits a day before it — and if the deadline moves, the
follow-up moves with it (§2.4 calls this through `after_due_change`).

Won, Lost and On Hold are exempt (§3.1). A closed lead with a
follow-up date is how a dead lead keeps appearing on somebody's
morning list for a year.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

log = logging.getLogger(__name__)

#: §3.1 — stages that never ask for a follow-up.
EXEMPT = ('Won', 'Lost', 'On Hold')

#: Sentinel in the settings table: not an interval, but "one day
#: before the quote due date".
BEFORE_DUE = -1

#: §3.3 — the quick picks, in the order they appear on the screen.
QUICK_PICKS = (
    ('tomorrow', 'Tomorrow', 1),
    ('plus3', '+3 days', 3),
    ('plus7', '+7 days', 7),
    ('after_due', 'After due date', None),
)


def _rules():
    from app.services import admin_settings as settings

    by_stage = settings.get('followup_days_by_stage') or {}
    if not isinstance(by_stage, dict):
        by_stage = {}
    try:
        fallback = int(settings.get('followup_default_days') or 3)
    except (TypeError, ValueError):
        fallback = 3
    return by_stage, max(1, fallback)


def is_exempt(stage):
    return (stage or '').strip() in EXEMPT


def days_for(stage):
    """The interval for a stage, or BEFORE_DUE, or None if exempt."""
    if is_exempt(stage):
        return None
    by_stage, fallback = _rules()
    stage = (stage or '').strip()
    if stage in by_stage:
        try:
            return int(by_stage[stage])
        except (TypeError, ValueError):
            return fallback
    # An alias stage follows whatever it is an alias of, so a CRM
    # that gains a stage name does not silently fall to the default.
    try:
        from app.services import stage_map
        canonical = stage_map.canonical(stage)
        if canonical != stage and canonical in by_stage:
            return int(by_stage[canonical])
    except Exception:                                     # noqa: BLE001
        pass
    return fallback


def propose(lead, *, stage=None, today=None):
    """The date to offer, or None when the stage is exempt.

    Never today and never in the past: a follow-up due the moment it
    is set is a follow-up that is already overdue, which is how 154
    of these ended up on an overdue list nobody could clear.
    """
    from app.services import quote_due as qd

    stage = stage if stage is not None else getattr(lead, 'stage', '')
    if is_exempt(stage):
        return None
    today = today or qd.today_ist()
    days = days_for(stage)

    if days == BEFORE_DUE:
        due = getattr(lead, 'quote_due_at', None)
        if due is not None:
            # A day before the deadline, in IST — the deadline is
            # stored in UTC and "the day before" is a working day,
            # not a 24-hour subtraction.
            local = qd.to_ist(due).date()
            proposed = local - timedelta(days=1)
            return max(proposed, today + timedelta(days=1))
        # No due date yet. §2.2 refuses the save without one, so this
        # is the order the screens happen to ask in, not a gap: a day
        # is the shortest honest answer until the deadline is known.
        days = 1

    return today + timedelta(days=max(1, int(days)))


def quick_picks(lead, *, today=None):
    """[(key, label, date)] for the screen's one-tap buttons."""
    from app.services import quote_due as qd

    today = today or qd.today_ist()
    out = []
    for key, label, days in QUICK_PICKS:
        if days is not None:
            out.append((key, label, today + timedelta(days=days)))
            continue
        due = getattr(lead, 'quote_due_at', None)
        if due is None:
            continue        # nothing to be after
        after = qd.to_ist(due).date() + timedelta(days=1)
        out.append((key, label, max(after, today + timedelta(days=1))))
    return out


def missing(lead):
    """Does this lead owe a follow-up date? (§3.5 counts these.)"""
    if getattr(lead, 'is_archived', False):
        return False
    if is_exempt(getattr(lead, 'stage', '')):
        return False
    return getattr(lead, 'followup_date', None) is None


def after_due_change(lead, *, was=None, today=None, actor=None):
    """Keep an RFQ-stage follow-up a day ahead of a moved deadline.

    `was` is the deadline the lead carried before the change, and the
    whole decision turns on it. A follow-up sitting exactly a day
    before the old deadline is the rule's own date — this module put
    it there — so it moves with the deadline. Any other date was
    chosen by a person, and a person's date is a decision, not a
    default to be recalculated under them.

    Without `was` there is nothing to compare against, so only a
    missing or already-stale follow-up is touched. (The first version
    of this compared the follow-up with the *new* deadline, which is
    almost always earlier — so the guard was true every time and the
    function did nothing at all. A test caught it; it would otherwise
    have looked like it worked, because the field it writes is usually
    already right.)
    """
    if days_for(getattr(lead, 'stage', '')) != BEFORE_DUE:
        return None
    due = getattr(lead, 'quote_due_at', None)
    if due is None:
        return None
    from app.services import quote_due as qd

    current = getattr(lead, 'followup_date', None)
    deadline = qd.to_ist(due).date()
    if current is not None:
        if was is not None:
            rules_own = qd.to_ist(was).date() - timedelta(days=1)
            if current != rules_own:
                return None     # somebody chose this date
        elif current < deadline:
            return None         # still ahead of the clock
    wanted = propose(lead, today=today)
    if wanted and wanted != current:
        lead.followup_date = wanted
        log.info('lead %s follow-up moved to %s with its due date',
                 getattr(lead, 'id', '?'), wanted)
        return wanted
    return None
