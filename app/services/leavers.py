"""Somebody who has left the company, as against somebody switched off.

`employees.is_active` answers "can this person sign in". It is false
for a suspension, for a long leave, for an account created by mistake
and for somebody who has left, and nothing could tell those apart. So
the Employee Master's Activate button would put a departed person back
into every picker with one click, and no screen or report could say
which of the inactive rows were leavers.

`employees.left_on` is the durable fact. Setting it:

    does not by itself stop anything -- `is_active` false does that,
    and `mark_left` sets both, bumps `session_version` so every open
    session of theirs ends, and writes an audit row;

    makes reactivation deliberate: `refuse_reactivation` is asked by
    /api/employees before honouring `is_active: true`, so putting a
    leaver back takes clearing the leaver mark as well, which is a
    decision somebody makes rather than a button they press;

    gives every picker and every refusal something true to say --
    "has left Procam" rather than "is not an active employee".

The enforcement itself is already everywhere: every candidate query in
the CRM filters on `is_active`, and the two assignment write paths
(`app/services/lead_assignment.validate` and
`app/directory/service._active_employee`) refuse an inactive person.
This module does not add a second gate beside those; it makes the one
that exists mean the right thing.
"""
from __future__ import annotations

import logging
from datetime import date

log = logging.getLogger(__name__)

ACTION = 'employee.left'
ACTION_UNDO = 'employee.left_undo'


def _employee(code_or_emp):
    from app import Employee
    if code_or_emp is None or isinstance(code_or_emp, Employee):
        return code_or_emp
    code = str(code_or_emp).strip().upper()
    if not code:
        return None
    return Employee.query.filter_by(emp_code=code).first()


def has_left(code_or_emp):
    """True when this person is recorded as having left."""
    emp = _employee(code_or_emp)
    return bool(emp is not None and getattr(emp, 'left_on', None))


def left_on(code_or_emp):
    emp = _employee(code_or_emp)
    return getattr(emp, 'left_on', None) if emp is not None else None


def why_not_assignable(code_or_emp, label='This person'):
    """The reason they cannot take work, or None if they can.

    Two different sentences on purpose. "Not active" sends somebody to
    Employee Master to switch the account on; "has left" tells them to
    pick a different person, which is the answer that is wanted.
    """
    emp = _employee(code_or_emp)
    if emp is None:
        return f'{label} is not an employee.'
    if getattr(emp, 'left_on', None):
        return (f'{emp.name} has left Procam ({emp.left_on}) and cannot be '
                f'given work. Choose somebody else.')
    if not emp.is_active:
        return (f'{emp.name} is not an active employee — the work would sit '
                f'with nobody.')
    return None


def refuse_reactivation(emp, payload):
    """Why `payload` may not switch this account back on, or None.

    Asked by /api/employees before it honours `is_active: true`. Putting
    a leaver back has to clear the leaver mark in the same breath, so
    somebody states that they have returned rather than pressing a
    button that means something else.
    """
    if not getattr(emp, 'left_on', None):
        return None
    if payload.get('is_active') is not True:
        return None
    if 'left_on' in payload and not payload.get('left_on'):
        return None                       # clearing it in the same save
    return (f'{emp.name} is recorded as having left Procam on '
            f'{emp.left_on}. Clear that first if they have come back — '
            f'reactivating a leaver by itself would put them back into '
            f'every assignment list.')


def mark_left(code_or_emp, *, actor, note='', when=None, batch=''):
    """Record that somebody has left. Returns (ok, error).

    Does not commit: the caller owns the transaction, as everywhere
    else in this codebase.
    """
    from app import db
    from app.services import audit

    emp = _employee(code_or_emp)
    if emp is None:
        return False, 'no such employee'
    was = {'is_active': bool(emp.is_active),
           'left_on': str(emp.left_on) if emp.left_on else '',
           'left_note': emp.left_note or ''}
    emp.left_on = when or date.today()
    emp.left_note = (note or '')[:200] or None
    emp.is_active = False
    # Every session they still hold ends now, not when the cookie expires.
    emp.session_version = (emp.session_version or 0) + 1
    audit.record(ACTION, 'employee', emp.emp_code, old=was,
                 new={'is_active': False, 'left_on': str(emp.left_on),
                      'left_note': emp.left_note or '', 'batch': batch},
                 actor=actor, reason=(note or 'left the company')[:400],
                 commit=False)
    try:
        db.session.flush()
    except Exception:
        db.session.rollback()
        raise
    return True, None


def unmark(code_or_emp, *, actor, batch=''):
    """Undo `mark_left`: the leaver mark and the active flag as they were.

    `session_version` is deliberately NOT put back. It is a revocation,
    and un-revoking sessions that were ended is not an undo of a
    mistake, it is a second mistake.
    """
    from app.models.audit import AuditEvent
    from app.services import audit

    emp = _employee(code_or_emp)
    if emp is None:
        return False, 'no such employee'
    event = (AuditEvent.query
             .filter(AuditEvent.action == ACTION,
                     AuditEvent.entity_id == emp.emp_code)
             .order_by(AuditEvent.id.desc()).first())
    was = (event.old_value if event is not None
           and isinstance(event.old_value, dict) else {})
    emp.left_on = None
    emp.left_note = None
    emp.is_active = bool(was.get('is_active', True))
    audit.record(ACTION_UNDO, 'employee', emp.emp_code,
                 old={'left_on': str(event.new_value.get('left_on'))
                      if event is not None and
                      isinstance(event.new_value, dict) else ''},
                 new={'is_active': emp.is_active, 'left_on': '',
                      'batch': batch},
                 actor=actor, reason='the leaver mark was removed',
                 commit=False)
    return True, None
