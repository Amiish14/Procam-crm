"""
v2026-08 — Pre-Sales services · Phase 1.

Business-logic helpers that centralise the invariants around assignment,
stage transitions and activity logging so that any route (and any future
UI) can rely on the same behaviour.
"""
from datetime import datetime
from typing import Optional

from app import db, Company
from presales.models import (
    AccountAssignmentHistory, AccountStageHistory,
    AccountActivity, ACCOUNT_DEV_STAGES, ACTIVITY_KINDS,
)


class PreSalesError(Exception):
    """Domain error surfaced back to the API as a 400."""


# ─── Assignment ────────────────────────────────────────────────────────
def reassign_account(*, account: Company, new_pic_code: str,
                     changed_by_code: str,
                     reason: Optional[str] = None) -> AccountAssignmentHistory:
    """Change an Account's PIC, appending a row to account_assignments.
    Never overwrites history."""
    new_pic_code = (new_pic_code or '').strip()
    if not new_pic_code:
        raise PreSalesError('new_pic_code is required')
    prev = getattr(account, 'pic_emp_code', None)
    account.pic_emp_code = new_pic_code
    row = AccountAssignmentHistory(
        account_id        = account.id,
        previous_pic_code = prev,
        new_pic_code      = new_pic_code,
        assigned_by       = changed_by_code,
        reason            = reason,
    )
    db.session.add(row)
    return row


# ─── Stage transitions ────────────────────────────────────────────────
def change_account_stage(*, account: Company, new_stage: str,
                         changed_by_code: str,
                         note: Optional[str] = None) -> AccountStageHistory:
    new_stage = (new_stage or '').strip()
    if new_stage not in ACCOUNT_DEV_STAGES:
        raise PreSalesError(f'Unknown account dev stage: {new_stage!r}')
    prev = getattr(account, 'dev_stage', None)
    account.dev_stage = new_stage
    row = AccountStageHistory(
        account_id  = account.id,
        from_stage  = prev,
        to_stage    = new_stage,
        changed_by  = changed_by_code,
        note        = note,
    )
    db.session.add(row)
    return row


# ─── Activity ─────────────────────────────────────────────────────────
def log_activity(*, account: Company, kind: str, performed_by_code: str,
                 subject: Optional[str] = None,
                 body: Optional[str] = None,
                 contact_id: Optional[int] = None,
                 occurred_at: Optional[datetime] = None,
                 next_action: Optional[str] = None,
                 next_action_at=None) -> AccountActivity:
    if kind not in ACTIVITY_KINDS:
        # Don't reject — coerce to a safe default. The spec allows admin
        # to extend the vocabulary later; being strict here would break
        # future extensibility for no security benefit.
        kind = 'Other'
    row = AccountActivity(
        account_id     = account.id,
        contact_id     = contact_id,
        kind           = kind,
        subject        = (subject or '').strip() or None,
        body           = (body or '').strip() or None,
        occurred_at    = occurred_at or datetime.utcnow(),
        performed_by   = performed_by_code,
        next_action    = (next_action or '').strip() or None,
        next_action_at = next_action_at,
    )
    db.session.add(row)
    # Bubble up to Account for dashboard "last activity" filters.
    account.last_activity_at = row.occurred_at
    if row.next_action_at and (not getattr(account, 'next_action_at', None)
                               or row.next_action_at < account.next_action_at):
        account.next_action_at = row.next_action_at
    return row


# ─── Read helpers ──────────────────────────────────────────────────────
def account_summary(account: Company) -> dict:
    """Combined snapshot for the Account detail page.

    Lead.company is a free-text field (legacy). Opportunity.company_id is
    the proper FK. We use both to catch legacy rows referenced by name
    only AND well-formed rows referenced by FK."""
    from app import Lead, Opportunity                             # local import
    lead_count = Lead.query.filter(
        db.func.lower(Lead.company) == (account.name or '').lower()
    ).count()
    opp_q = Opportunity.query.filter(Opportunity.company_id == account.id)
    opp_won   = opp_q.filter(Opportunity.stage.in_(['Won', 'Closed Won'])).count()
    opp_total = opp_q.count()
    win_rate = round(opp_won / opp_total * 100, 1) if opp_total else 0.0
    return {
        'account_id':   account.id,
        'lead_count':   lead_count,
        'opp_total':    opp_total,
        'opp_won':      opp_won,
        'win_rate_pct': win_rate,
        'dev_stage':    getattr(account, 'dev_stage', None),
        'pic':          getattr(account, 'pic_emp_code', None),
        'last_activity_at': (str(account.last_activity_at)[:16]
                             if getattr(account, 'last_activity_at', None) else None),
        'next_action_at':   (str(account.next_action_at)
                             if getattr(account, 'next_action_at', None) else None),
    }
