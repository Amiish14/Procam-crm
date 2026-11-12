"""Who may put a name on a lead: the owner, the second PIC, the vertical.

CRM-MAIL-02 2. The RFQ desk changed the owner and the secondary PIC on
the Fox Brasil lead and the change did not happen. Not refused -
*ignored*. `api_update_lead` reads those two fields only
`if _matrix_can('admin.triage')`, and silently drops them otherwise,
so the one action that gets a lead out of "nobody is accountable for
this lead" was available to administrators alone and failed quietly
for everybody else.

WHY THIS IS NOT JUST ANOTHER PERMISSION

Because of what it is the way out of. A lead with no owner is in the
worst state the CRM has: it is on no board, in nobody's queue, and
every rule that asks "is this yours" answers no - including the
account gate's own exemption. The people most likely to find such a
lead are the RFQ desk, who register the enquiry, and the vertical
head, who answers for the number. Making them ask an administrator to
type a name turns a ten-second fix into a day, which is exactly what
happened.

WHO, AND WHY EACH

    admin.triage / admin.master   as before. Nothing is taken away.
    the lead's vertical head      or its deputy, from the vertical
                                  master - the table the business
                                  actually maintains, not a role
                                  string somebody forgot to update.
    the RFQ desk                  the coordinator, and the SCM and
                                  operations desks for this lead's
                                  vertical, read from the Admin
                                  settings the RFQ chain already
                                  routes on.
    the lead's own owner          they may hand it on, and a lead with
                                  an owner is not the problem this is
                                  for. Not the secondary PIC: a
                                  monitor who can reassign the lead
                                  they are monitoring is not a monitor.

NO CODES IN HERE

Every name is read at the time: the vertical master for the heads,
the Admin settings for the desks, the lead itself for the owner. This
module holds the rule and nothing else, which is what lets a director
change the desk on a screen.

A REFUSAL IS SAID OUT LOUD

`may_set_people` returns (allowed, why). The caller must use the `why`
- a change that is dropped in silence is how this bug lasted, and the
same silence would hide the next one.
"""
from __future__ import annotations

#: The Admin settings that name the desks. One code each, except the
#: by-vertical ones which hold a mapping.
DESK_SETTING = 'rfq_coordinator'
DESK_BY_VERTICAL = ('rfq_scm_by_vertical', 'rfq_ops_by_vertical')

#: The permissions that have always been able to do this.
ADMIN_PERMS = ('admin.triage', 'admin.master')

#: Why somebody may, in words for a screen.
AS_ADMIN = 'a CRM administrator'
AS_HEAD = 'the head of this lead\'s vertical'
AS_DESK = 'the RFQ desk'
AS_OWNER = 'the lead\'s own owner'


def _code(value):
    return (str(value or '').strip().upper()) or ''


def _me(emp_code=None):
    from flask import has_request_context, session

    if emp_code is not None:
        return _code(emp_code)
    if has_request_context():
        return _code(session.get('emp_code'))
    return ''


def _vertical_of(lead, vertical=None):
    """The lead's own vertical, or the one being set in this save."""
    from app.services import account_gate

    return account_gate.vertical_of(lead, override=vertical)


def head_codes(vertical):
    """The head and deputy of a vertical, from the vertical master."""
    out = set()
    if not (vertical or '').strip():
        return out
    try:
        from app.services import vertical_heads
        for code in vertical_heads.heads_for_vertical(vertical) or []:
            if _code(code):
                out.add(_code(code))
    except Exception:                                     # noqa: BLE001
        pass
    return out


def desk_codes(vertical=''):
    """The RFQ desks: the coordinator, and this vertical's SCM and ops."""
    out = set()
    try:
        from app.services import admin_settings
    except Exception:                                     # noqa: BLE001
        return out
    try:
        code = _code(admin_settings.get(DESK_SETTING))
        if code:
            out.add(code)
    except Exception:                                     # noqa: BLE001
        pass
    for key in DESK_BY_VERTICAL:
        try:
            mapping = admin_settings.get(key)
        except Exception:                                 # noqa: BLE001
            continue
        if isinstance(mapping, dict):
            # The by-vertical settings are keyed by vertical name. The
            # whole desk counts, not only this lead's vertical: an RFQ
            # coordinator who may fix an Air lead may fix a Heavy
            # Transport one, and the alternative is a lead nobody at
            # all can put a name on.
            for value in mapping.values():
                if _code(value):
                    out.add(_code(value))
        elif _code(mapping):
            out.add(_code(mapping))
    return out


def may_set_people(lead, emp_code=None, *, vertical=None):
    """(allowed, why) for changing the owner, second PIC or vertical.

    `vertical` is the one being set in the same save, when it is
    changing: the head of the vertical a lead is *moving to* is as
    entitled as the head of the one it is leaving, and on this lead
    those were different people.
    """
    code = _me(emp_code)
    if not code:
        return False, 'Sign in first.'

    perms = set()
    try:
        from app.access.service import effective
        _scope, perms = effective(code)
    except Exception:                                     # noqa: BLE001
        perms = set()
    if any(p in perms for p in ADMIN_PERMS):
        return True, AS_ADMIN

    if lead is not None and _code(getattr(lead, 'assigned_to', '')) == code:
        return True, AS_OWNER

    verticals = {_vertical_of(lead)}
    if vertical:
        verticals.add(_vertical_of(lead, vertical=vertical))
    for name in verticals:
        if name and code in head_codes(name):
            return True, AS_HEAD

    if code in desk_codes(_vertical_of(lead, vertical=vertical)):
        return True, AS_DESK

    return False, ('Only a CRM administrator, the head of this lead\'s '
                   'vertical or the RFQ desk can change who a lead '
                   'belongs to. The owner, the second PIC and the '
                   'vertical were left as they were — everything else '
                   'in this save went through.')


def may_clear_account_question(lead, emp_code=None, *, vertical=None):
    """Whether this person can settle who owns the account for a lead.

    The same three: an administrator, the vertical head, the RFQ
    desk. Used by `account_gate` so its refusal cannot name somebody
    and refuse that same person in one sentence — "ask the head of
    Air Freight" told to the head of Air Freight is a loop.

    Not the lead's own owner: naming an owner on an *account* is a
    decision about the customer, which is D6's rule and stays with
    the head and the administrators.
    """
    code = _me(emp_code)
    if not code:
        return False
    try:
        from app.access.service import effective
        _scope, perms = effective(code)
        if any(p in perms for p in ADMIN_PERMS):
            return True
    except Exception:                                     # noqa: BLE001
        pass
    names = {_vertical_of(lead)}
    if vertical:
        names.add(_vertical_of(lead, vertical=vertical))
    for name in names:
        if name and code in head_codes(name):
            return True
    return code in desk_codes(_vertical_of(lead, vertical=vertical))
