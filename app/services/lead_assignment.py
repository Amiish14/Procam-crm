"""
Lead assignment — primary and secondary PIC.

A lead has one owner who works it and, optionally, one monitor who is
expected to notice when it is not being worked.  Both are recorded, both
are notified, and every change is written to lead_assignment_history.

Everything that assigns a lead goes through `assign()`:

    the lead PUT endpoint, bulk assign, the Admin triage dashboard

so the validation, the history row and the two notifications cannot be
skipped by using one path instead of another.
"""
from datetime import datetime


def _employee(code):
    from app import Employee
    code = (code or '').strip()
    if not code:
        return None
    return Employee.query.filter_by(emp_code=code).first()


def validate(primary_code, secondary_code):
    """Return an error string, or None when the pair is assignable."""
    primary = (primary_code or '').strip()
    secondary = (secondary_code or '').strip()

    if secondary and secondary == primary:
        return ('The same person cannot be both primary and secondary PIC — '
                'the secondary exists to notice what the primary misses.')

    for code, label in ((primary, 'Primary'), (secondary, 'Secondary')):
        if not code:
            continue
        emp = _employee(code)
        if emp is None:
            return f'{label} PIC {code} is not an employee.'
        if not emp.is_active:
            return (f'{label} PIC {emp.name} is not an active employee — '
                    f'the lead would sit with nobody.')
    return None


def assign(lead, primary_code=None, secondary_code=None, actor=None,
           note=None, notify=True, _defer_commit=False):
    """Set the primary and/or secondary PIC on a lead.

    Pass only what is changing: `None` leaves a side alone, `''` clears
    it.  Returns (ok, error).  Nothing is committed here — the caller
    owns the transaction — but the history row is added to the session.
    """
    from app import db, LeadAssignmentHistory

    old_primary = lead.assigned_to or ''
    old_secondary = lead.secondary_owner or ''

    new_primary = (old_primary if primary_code is None
                   else (primary_code or '').strip())
    new_secondary = (old_secondary if secondary_code is None
                     else (secondary_code or '').strip())

    if new_primary == old_primary and new_secondary == old_secondary:
        return True, None                       # nothing to do, no history

    err = validate(new_primary, new_secondary)
    if err:
        return False, err

    primary_emp = _employee(new_primary)
    secondary_emp = _employee(new_secondary)

    lead.assigned_to = new_primary or None
    lead.assigned_name = (primary_emp.name if primary_emp
                          else (new_primary or None))
    lead.secondary_owner = new_secondary or None
    lead.secondary_owner_name = (secondary_emp.name if secondary_emp
                                 else (new_secondary or None))

    db.session.add(LeadAssignmentHistory(
        lead_id=lead.id,
        from_primary=old_primary or None, to_primary=new_primary or None,
        from_secondary=old_secondary or None,
        to_secondary=new_secondary or None,
        changed_at=datetime.utcnow(), changed_by=actor, note=note))

    if notify:
        # Only the people whose own role changed — reassigning the
        # secondary should not re-notify a primary who has held the lead
        # for a month.
        if new_primary and new_primary != old_primary:
            _notify(lead, primary_emp, new_primary, is_primary=True,
                    actor=actor)
        if new_secondary and new_secondary != old_secondary:
            _notify(lead, secondary_emp, new_secondary, is_primary=False,
                    actor=actor)
    return True, None


def _notify(lead, employee, emp_code, is_primary, actor=None):
    """In-app notification, and the assignment email that already existed.

    Best-effort on both counts: a notification failure must never roll
    back the assignment or surface to the caller.
    """
    from app import db, app as flask_app
    from app.models.notification import Notification

    who = lead.company or f'Lead #{lead.id}'
    if is_primary:
        title = f'Lead assigned to you — {who}'
        body = ('You are the primary PIC. This lead is yours to progress.')
    else:
        # The distinction has to be unmissable, or a monitor reads it as
        # "this is mine" and two people work the same lead, or neither.
        title = f'You are monitoring a lead — {who}'
        body = (f'You are the secondary PIC (monitor/backup). '
                f'{lead.assigned_name or "The primary PIC"} owns this lead; '
                f'you are here to pick it up if it stalls.')
    if actor:
        body += f' Assigned by {actor}.'

    try:
        db.session.add(Notification(
            user_id=emp_code,
            kind='lead_assigned',
            title=title, body=body,
            entity_type='Lead', entity_id=lead.id,
            action_url=f'/app?lead={lead.id}'))
    except Exception:
        try:
            flask_app.logger.exception(
                'in-app assignment notification failed for lead %s', lead.id)
        except Exception:
            pass

    # The CRM already emails the primary on assignment; the secondary
    # gets the same courtesy, with the monitor wording above.
    if employee is not None:
        try:
            from email_ingest import notifier
            notifier.notify_lead_assigned(lead, employee,
                                          assigned_by=actor or '')
        except Exception:
            try:
                flask_app.logger.exception(
                    'assignment email failed for lead %s', lead.id)
            except Exception:
                pass


def history_for(lead_id, limit=50):
    from app import LeadAssignmentHistory
    rows = (LeadAssignmentHistory.query
            .filter_by(lead_id=lead_id)
            .order_by(LeadAssignmentHistory.changed_at.desc())
            .limit(limit).all())
    return [r.to_dict() for r in rows]
