"""The states that close a task, and why there is more than one.

CRM-FINAL-CLOSEOUT-01 §10 (fault 3, docs/operations/
RELEASE_2026_10_TASKS.md). `lead.rfq_review` and `lead.qualify` pile
up because each waits on exactly one stage name, and a lead is under
no obligation to pass through it.

WHAT WAS WRONG WITH EACH

`lead.qualify` waits on `Lead / Profile Sent`. **That stage no longer
exists.** `stage_map.ALIASES` retired it onto Call Done, so nothing
writes it any more and the task can never complete — the October
release note has it as "closes on Profile Sent ✓", which was true
when it was written and stopped being true when the stage was
renamed. A task whose closing state has been renamed out of
existence is immortal and nothing says so.

`lead.rfq_review` waits on `Lead / Quoted`. That stage does exist,
but a lead is not required to visit it: RFQ Generated → Under
Negotiation happens when the customer negotiates off a verbal, and
RFQ Generated → Lost happens when they go elsewhere. Either way the
review task stays open on a lead nobody is working.

THE RULE: PAST THE POINT, NOT AT IT

So each task closes on **any** state at or beyond the point its work
belongs to, plus the terminal states. A lead that reached Won
obviously got qualified; a lead marked Lost obviously will not be.
Both are answers, and an open task is not.

The lists are computed from `stage_map.JOURNEY` and
`stage_map.TERMINAL` rather than typed out, because a typed list is
how this broke the first time: somebody renamed a stage and the task
definition went on naming the old one. A test asserts the computed
lists cover every stage on the journey, so adding a stage fails there
instead of quietly making a task immortal again.

COMPLETED OR CANCELLED

A backfill has to say which. Where the lead advanced through the
journey the work happened — the lead got quoted, so the RFQ was
reviewed — and the row is **Completed**. Where the lead died the work
became moot and nobody did it, so the row is **Cancelled**. Marking
all of them Completed would record work nobody did; marking all of
them Cancelled would erase work that was done.
"""
from __future__ import annotations

#: The two task definitions this fixes, and the journey stage each
#: one's work belongs to. The task closes at that stage or beyond.
#:
#: No stage name is spelled out as a closing state anywhere below —
#: only the point on the journey each task belongs to, which is the
#: thing that does not change when a stage is renamed.
AFTER = {
    'lead.qualify': 'New',
    'lead.rfq_review': 'RFQ Generated',
}

#: What each one is waiting on today, so a check run can show the
#: before and the after rather than only the after.
WAS = {
    'lead.qualify': 'Profile Sent',
    'lead.rfq_review': 'Quoted',
}


def _journey():
    from app.services import stage_map
    return stage_map


def closing_states(task_key):
    """Every state that should close this task, in journey order.

    For `lead.qualify`, whose point is 'New': everything after New,
    because leaving New *is* being qualified — and the terminal
    states, because a lead that was disqualified or closed has been
    answered.
    """
    stage_map = _journey()
    anchor = AFTER.get(task_key)
    if anchor is None:
        return ()
    try:
        index = stage_map.JOURNEY.index(anchor)
    except ValueError:                                    # pragma: no cover
        return ()

    # Strictly after the anchor for `lead.qualify` (leaving New is the
    # qualification) and for `lead.rfq_review` (the review is done
    # once the lead has moved on from RFQ Generated).
    out = list(stage_map.JOURNEY[index + 1:])
    for state in stage_map.TERMINAL:
        if state not in out:
            out.append(state)
    return tuple(out)


def completion_rule(task_key):
    """The `completion_state` to store on the TaskDefinition row."""
    states = closing_states(task_key)
    if not states:
        return None
    return {'entity': 'Lead', 'states': list(states)}


def closes_now(task_key, stage):
    """Whether a lead on this stage should already have closed it.

    Folded through `stage_map.canonical`, so a lead still written
    'Visit Done' is read as Business Discussion — the whole reason
    `lead.qualify` became immortal was a stage name that moved.
    """
    stage_map = _journey()
    current = stage_map.canonical(stage)
    if not current:
        return False
    return current in {stage_map.canonical(s)
                       for s in closing_states(task_key)}


def outcome_for(stage):
    """('Completed'|'Cancelled', why) for a row closed by the sweep.

    See the module docstring: the lead advancing means the work
    happened, and the lead dying means it did not.
    """
    from app.models.task_engine import TaskInstanceStatus
    from app.services import stage_map

    current = stage_map.canonical(stage)
    if current in stage_map.TERMINAL and current != 'Won':
        return (TaskInstanceStatus.CANCELLED,
                f'the lead is {current} — this was never done and '
                f'will not be')
    return (TaskInstanceStatus.COMPLETED,
            f'the lead reached {current or "a later stage"}, which it '
            f'could not have done without this')
