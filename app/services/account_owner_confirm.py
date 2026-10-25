"""
Ask who serves this account once, not once per lead.

CRM-ACCOUNT-FIRST-01 §3.4 (D4).

`auto_assign.open_confirmation` asks per lead, and it is right to:
it exists for the case where a *fallback* rule picked an owner and
somebody should check that one lead. But once a head has said who
serves Tata Steel for Heavy Transport, the next forty Tata Steel
leads do not each deserve their own question — forty identical
questions is how a digest becomes something people archive unread.

So the question moves up a level. The per-lead table keeps
answering what it was built for and is not touched; this answers
the account-level one beside it, and a lead whose account and
vertical already have an answer inherits it and asks nothing.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def answered(account_id, vertical):
    """The confirmed owner for this account × vertical, or ''."""
    from app.models.assignment import AccountOwnerConfirmation

    if not account_id or not (vertical or '').strip():
        return ''
    try:
        row = AccountOwnerConfirmation.query.filter_by(
            account_id=account_id, vertical=vertical.strip()).first()
    except Exception:                                     # noqa: BLE001
        from app import db
        db.session.rollback()
        return ''
    if row is None or row.confirmed_at is None:
        return ''
    return row.reassigned_to or row.emp_code or ''


def ask(account_id, vertical, emp_code, *, rule='', lead_id=None):
    """Open the question, once. Returns the row, or None.

    Idempotent on (account, vertical): a second lead from the same
    customer adds nothing. An answered question is never reopened —
    that is the whole point of moving it up here.
    """
    from app import db
    from app.models.assignment import AccountOwnerConfirmation

    if not account_id or not (vertical or '').strip() or not emp_code:
        return None
    vertical = vertical.strip()
    try:
        row = AccountOwnerConfirmation.query.filter_by(
            account_id=account_id, vertical=vertical).first()
        if row is not None:
            return None if row.confirmed_at is not None else row
        row = AccountOwnerConfirmation(
            account_id=account_id, vertical=vertical,
            emp_code=emp_code, rule=rule, first_lead_id=lead_id)
        db.session.add(row)
        return row
    except Exception:                                     # noqa: BLE001
        db.session.rollback()
        log.exception('could not open an account confirmation for '
                      '%s / %s', account_id, vertical)
        return None


def pending_for(emp_code, *, limit=200):
    """What this person is being asked to confirm, account by account."""
    from app.models.assignment import AccountOwnerConfirmation

    try:
        return (AccountOwnerConfirmation.query
                .filter(AccountOwnerConfirmation.emp_code == emp_code,
                        AccountOwnerConfirmation.confirmed_at.is_(None))
                .order_by(AccountOwnerConfirmation.created_at)
                .limit(limit).all())
    except Exception:                                     # noqa: BLE001
        from app import db
        db.session.rollback()
        return []


def answer(account_id, vertical, *, outcome, by, reassign_to=None,
           commit=True):
    """Confirm the owner for an account × vertical, or move it.

    Reassigning writes `AccountVerticalOwner` too, because the
    correction is the decision — leaving the answer in a
    confirmation row and not in the owner table would mean the next
    lead asks again.
    """
    from datetime import datetime

    from app import db
    from app.models.assignment import (AccountOwnerConfirmation,
                                       AccountVerticalOwner)
    from app.services import audit

    row = AccountOwnerConfirmation.query.filter_by(
        account_id=account_id, vertical=(vertical or '').strip()).first()
    if row is None:
        return False, 'nothing to answer'
    if row.confirmed_at is not None:
        return False, 'already answered'
    if outcome not in ('confirmed', 'reassigned'):
        return False, 'outcome must be confirmed or reassigned'
    if outcome == 'reassigned' and not (reassign_to or '').strip():
        return False, 'name the person it moves to'

    owner = (reassign_to or '').strip() if outcome == 'reassigned' \
        else row.emp_code
    row.confirmed_at = datetime.utcnow()
    row.confirmed_by = by
    row.outcome = outcome
    row.reassigned_to = (reassign_to or '').strip() or None

    existing = AccountVerticalOwner.query.filter_by(
        account_id=account_id, vertical=row.vertical).first()
    if existing is None:
        db.session.add(AccountVerticalOwner(
            account_id=account_id, vertical=row.vertical,
            emp_code=owner, source='manual',
            note='confirmed by the vertical head'))
    elif existing.emp_code != owner:
        existing.emp_code = owner
        existing.source = 'manual'

    audit.record('account.owner_confirmed', 'company', account_id,
                 new={'vertical': row.vertical, 'emp_code': owner,
                      'outcome': outcome},
                 actor=by, reason='answered on the owner digest')
    if commit:
        db.session.commit()
    return True, ''
