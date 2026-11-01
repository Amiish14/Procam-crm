"""
One scope per screen, and the label that says which one.

CRM-COCKPIT-02 §4.3. The cockpit showed two overdue numbers — 154 on
the My Work strip and 128 in the KPI row — with nothing on screen to
say they were measuring different populations. Neither was wrong: the
front end appends `?scope=mailbox` to a short list of endpoints
(`CRM.dataScope`, pinned since September, so lead lists show only what
arrived through leads@), and the strip's endpoint was not on that list.
So one number counted leads that came in by mail and the other counted
every lead the viewer's team owns, and the difference read as a bug in
the CRM to everybody who saw it.

Three scopes, named the way a person would:

    Mailbox   leads that arrived through leads@procamgroup.in
    Team      every lead inside the viewer's own access boundary
    Mine      only leads the viewer owns

The access boundary is not negotiable and is applied underneath all
three — this narrows *within* what somebody may already see, it never
widens it.

The rule this enforces: an endpoint that answers a cockpit number
takes its scope from here, returns the label beside the number, and
two numbers with the same label are over the same population. A tile
whose scope differs from its screen's prints its own label, which is
the other half of §4.3.
"""
from __future__ import annotations

MAILBOX = 'mailbox'
TEAM = 'team'
MINE = 'mine'
SCOPES = (MAILBOX, TEAM, MINE)

LABELS = {MAILBOX: 'Mailbox', TEAM: 'Team', MINE: 'Mine'}

#: What a screen gets when it asks for nothing: the whole access
#: boundary, which is exactly what every endpoint returned before this
#: module existed.
#:
#: Deliberately not MAILBOX, although `CRM.dataScope` pins the lead
#: lists to the mailbox and it was tempting to make the server agree
#: by default. The Workbench, the Overview tiles and the copilot all
#: read these same endpoints and have always counted every lead in
#: scope; a mailbox default would have hidden every manually created
#: and imported lead from them, silently, as a side effect of fixing
#: two numbers on one screen. Instead the *front end* asks for the
#: same scope everywhere (§4.3), the server answers what it is asked,
#: and the label says which it was.
DEFAULT = TEAM

#: The older spelling the lead list endpoints already accept. 'all'
#: means the whole access boundary, which is this module's TEAM.
ALIASES = {'all': TEAM, 'team': TEAM, '': DEFAULT, None: DEFAULT,
           'own': MINE, 'me': MINE}


def normalise(raw):
    value = (raw or '').strip().lower()
    if value in SCOPES:
        return value
    return ALIASES.get(value, DEFAULT)


def wanted(default=None):
    """The scope this request asked for."""
    try:
        from flask import request

        raw = request.args.get('scope')
    except Exception:                                     # noqa: BLE001
        raw = None
    if raw is None and default is not None:
        return normalise(default)
    return normalise(raw)


def label(scope=None):
    return LABELS.get(normalise(scope) if scope else wanted(), 'Team')


def is_mailbox(scope=None):
    return (normalise(scope) if scope else wanted()) == MAILBOX


def apply(q, scope=None, *, emp_code=None):
    """Narrow a lead query to a scope. The access boundary is the
    caller's business and is applied separately — this only adds the
    scope's own condition, so it can never widen anything."""
    from app import Lead

    scope = normalise(scope) if scope else wanted()
    if scope == MAILBOX:
        return q.filter(Lead.source == 'email',
                        Lead.email_message_id.isnot(None))
    if scope == MINE:
        code = emp_code
        if code is None:
            try:
                from flask import session
                code = session.get('emp_code')
            except Exception:                             # noqa: BLE001
                code = None
        if code:
            from sqlalchemy import or_
            return q.filter(or_(Lead.assigned_to == code,
                                Lead.secondary_owner == code))
    return q


def keeps(lead, scope=None, *, emp_code=None):
    """The same decision for one lead already in hand.

    The Workbench builds items in Python and its filters run over
    those, so the scope has to be answerable without a query or the
    tiles would go back to disagreeing with the lists.
    """
    scope = normalise(scope) if scope else wanted()
    if scope == MAILBOX:
        return ((getattr(lead, 'source', '') or '') == 'email'
                and bool(getattr(lead, 'email_message_id', None)))
    if scope == MINE:
        code = emp_code
        if code is None:
            try:
                from flask import session
                code = session.get('emp_code')
            except Exception:                             # noqa: BLE001
                code = None
        if not code:
            return True
        return code in (getattr(lead, 'assigned_to', None),
                        getattr(lead, 'secondary_owner', None))
    return True


def described(scope=None):
    """{scope, label, note} — what a screen prints beside a number."""
    scope = normalise(scope) if scope else wanted()
    notes = {
        MAILBOX: 'Leads that arrived through leads@procamgroup.in.',
        TEAM: 'Every lead you are allowed to see.',
        MINE: 'Only the leads you own.',
    }
    return {'scope': scope, 'label': LABELS[scope],
            'note': notes[scope]}
