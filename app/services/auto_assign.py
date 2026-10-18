"""
Giving a lead an owner, without anybody doing it by hand.

The rule, in one sentence: a lead goes to whoever serves that
customer for that kind of work; failing that, to whoever serves the
customer; failing that, to the head of the vertical; and failing
even that, to the person who answers for the CRM. It never goes to
nobody, and it never goes to somebody who has left.

Four things that were not true before:

    an account can have a different owner per vertical. Godrej's
    warehousing and Godrej's heavy transport are not the same
    salesperson, and one `pic_emp_code` cannot say so.

    a departed owner is replaced from a table a person maintains,
    not left on the lead and not guessed at.

    an unmatched new customer gets a real owner immediately — the
    head of whatever vertical the mail looks like — rather than
    sitting unowned waiting for somebody to notice. The head is
    asked to confirm in one morning digest, not one mail per lead.

    every automatic assignment says in the audit trail which rule
    chose it, what it matched, and who had it before.

Behind FEATURE_AUTO_ASSIGN. Off, `assign_for` returns None and the
intake behaves exactly as it did.
"""
from __future__ import annotations

import logging
from datetime import datetime

log = logging.getLogger(__name__)

#: Where a lead goes when there is no vertical and no account: the
#: person who answers for the CRM. Configurable in settings; this is
#: the fallback of last resort, not a preference.
BACKSTOP_SETTING = 'auto_assign_backstop'
DEFAULT_BACKSTOP = 'DIR12010'

#: The rules, in the order they are tried. Recorded on the audit row
#: so "why did this go to her" is answerable.
R_ACCOUNT_VERTICAL = 'account owner for this vertical'
R_ACCOUNT = 'account owner'
R_VERTICAL_HEAD = 'head of the vertical'
R_BACKSTOP = 'backstop — no vertical and no account'
R_REPLACED = 'replacement for a departed owner'


def enabled():
    try:
        from app.services import flags
        return flags.on('FEATURE_AUTO_ASSIGN')
    except Exception:
        return False


def backstop():
    try:
        from app.services import admin_settings
        code = (admin_settings.get(BACKSTOP_SETTING) or '').strip().upper()
        return code or DEFAULT_BACKSTOP
    except Exception:
        return DEFAULT_BACKSTOP


# ── people ───────────────────────────────────────────────────────────
def _active(code):
    """The employee, if they are one and still here."""
    from app import Employee
    code = (code or '').strip().upper()
    if not code:
        return None
    emp = Employee.query.filter_by(emp_code=code).first()
    return emp if (emp is not None and emp.is_active) else None


def replacement_for(code):
    """Who takes over from somebody who has left, or None.

    Followed through a chain — if A was replaced by B and B has since
    left too, the row for B applies — with a guard against a cycle
    somebody enters by accident.
    """
    from app.models.assignment import OwnerReplacement

    seen, current = set(), (code or '').strip().upper()
    for _hop in range(6):
        if not current or current in seen:
            return None
        seen.add(current)
        try:
            row = (OwnerReplacement.query
                   .filter_by(from_emp_code=current, active=True).first())
        except Exception:
            from app import db
            db.session.rollback()
            return None
        if row is None:
            return None
        nxt = (row.to_emp_code or '').strip().upper()
        if _active(nxt):
            return nxt
        current = nxt
    return None


def usable(code):
    """`code` if they are active; their replacement if not; else None."""
    if _active(code):
        return (code or '').strip().upper()
    return replacement_for(code)


# ── the account's owners ─────────────────────────────────────────────
def owner_for_vertical(account_id, vertical):
    """Who serves this account for this kind of work, or None."""
    from app.models.assignment import AccountVerticalOwner

    if not account_id or not (vertical or '').strip():
        return None
    try:
        row = (AccountVerticalOwner.query
               .filter_by(account_id=account_id,
                          vertical=vertical.strip()).first())
    except Exception:
        from app import db
        db.session.rollback()
        return None
    return row.emp_code if row is not None else None


def account_owner(account):
    return (getattr(account, 'pic_emp_code', '') or '').strip().upper() \
        or None


# ── the decision ─────────────────────────────────────────────────────
def decide(*, account=None, vertical='', sender_email=''):
    """(emp_code, rule, needs_confirmation) — who should own this.

    Never returns an inactive person and never returns None for the
    code: the backstop is the floor. `needs_confirmation` is True
    when the choice came from a fallback rather than from knowing
    the customer, which is what the morning digest asks about.
    """
    from app.services import vertical_heads as vh

    vertical = (vertical or '').strip()

    if account is not None:
        by_vertical = owner_for_vertical(account.id, vertical)
        if by_vertical:
            code = usable(by_vertical)
            if code:
                rule = (R_ACCOUNT_VERTICAL if code == by_vertical
                        else f'{R_ACCOUNT_VERTICAL}, {R_REPLACED}')
                return code, rule, False

        general = account_owner(account)
        if general:
            code = usable(general)
            if code:
                rule = (R_ACCOUNT if code == general
                        else f'{R_ACCOUNT}, {R_REPLACED}')
                return code, rule, False

    # Nobody known for this customer. The head of whatever vertical
    # the mail looks like owns it from this moment — a real owner,
    # not a placeholder — and is asked to confirm in the morning.
    if vertical:
        for head in vh.heads_for_vertical(vertical):
            code = usable(head)
            if code:
                return code, R_VERTICAL_HEAD, True

    code = usable(backstop()) or backstop()
    return code, R_BACKSTOP, True


def assign_for(lead, *, account=None, sender_email='', actor='system',
               commit=False):
    """Give this lead an owner. Returns the decision, or None.

    None means the flag is off or the lead already has a usable
    owner — in both cases nothing is written and the caller carries
    on exactly as before.
    """
    if not enabled():
        return None
    if lead is None:
        return None

    existing = (lead.assigned_to or '').strip().upper()
    if existing and _active(existing):
        return None

    code, rule, confirm = decide(
        account=account, vertical=(lead.procam_vertical or ''),
        sender_email=sender_email)
    if not code:
        return None

    was = existing or None
    try:
        from app import db
        from app.services import lead_assignment

        ok, err = lead_assignment.assign(
            lead, primary_code=code, actor=actor,
            note=f'auto-assigned: {rule}', _defer_commit=True)
        if not ok:
            log.warning('auto-assign refused for lead %s: %s', lead.id, err)
            return None

        _record(lead, code, rule, was, account, sender_email)
        if confirm:
            open_confirmation(lead, code, rule)
        if commit:
            db.session.commit()
    except Exception:
        log.exception('auto-assign failed for lead %s',
                      getattr(lead, 'id', '?'))
        return None
    return {'emp_code': code, 'rule': rule, 'needs_confirmation': confirm,
            'previous_owner': was,
            'account_id': getattr(account, 'id', None)}


def _record(lead, code, rule, was, account, sender_email):
    """The audit row: the rule, what it matched, and who had it."""
    try:
        from app.services import audit
        audit.record(
            'lead.auto_assigned', 'Lead', lead.id,
            old={'assigned_to': was},
            new={'assigned_to': code, 'rule': rule,
                 'matched_account_id': getattr(account, 'id', None),
                 'matched_account': getattr(account, 'name', None),
                 'sender': sender_email or None},
            actor='auto-assign',
            reason=f'assigned automatically — {rule}'[:200],
            commit=False)
    except Exception:
        log.exception('could not audit the auto-assignment of lead %s',
                      getattr(lead, 'id', '?'))


# ── confirmations ────────────────────────────────────────────────────
def open_confirmation(lead, code, rule):
    """Note that a fallback chose this owner, for the morning digest."""
    from app import db
    from app.models.assignment import OwnerConfirmation

    try:
        row = OwnerConfirmation.query.filter_by(lead_id=lead.id).first()
        if row is not None and row.confirmed_at is None:
            row.emp_code, row.rule = code, rule
            return row
        if row is not None:
            return None            # already answered; do not reopen
        row = OwnerConfirmation(lead_id=lead.id, emp_code=code, rule=rule,
                                vertical=(lead.procam_vertical or ''))
        db.session.add(row)
        return row
    except Exception:
        db.session.rollback()
        log.exception('could not open a confirmation for lead %s', lead.id)
        return None


def pending_for(emp_code, *, limit=200):
    """What this person is being asked to confirm."""
    from app.models.assignment import OwnerConfirmation

    try:
        return (OwnerConfirmation.query
                .filter(OwnerConfirmation.emp_code == emp_code,
                        OwnerConfirmation.confirmed_at.is_(None))
                .order_by(OwnerConfirmation.created_at).limit(limit).all())
    except Exception:
        from app import db
        db.session.rollback()
        return []


def answer(lead_id, *, outcome, by, reassign_to=None, commit=True):
    """Confirm the owner, or move the lead to somebody else.

    Reassigning also teaches the matcher: the correction is the
    strongest signal the CRM gets about who a customer belongs to.
    """
    from app import Lead, db
    from app.models.assignment import OwnerConfirmation

    row = OwnerConfirmation.query.filter_by(lead_id=lead_id).first()
    if row is None or row.confirmed_at is not None:
        return False, 'already answered'

    lead = db.session.get(Lead, lead_id)
    if lead is None:
        return False, 'no such lead'

    if outcome == 'reassigned':
        code = usable(reassign_to)
        if not code:
            return False, 'that person is not active'
        from app.services import lead_assignment
        ok, err = lead_assignment.assign(
            lead, primary_code=code, actor=by,
            note='reassigned from the owner-confirmation digest',
            _defer_commit=True)
        if not ok:
            return False, err
        row.reassigned_to = code

    row.outcome = outcome
    row.confirmed_at = datetime.utcnow()
    row.confirmed_by = by
    if commit:
        db.session.commit()
    return True, ''
