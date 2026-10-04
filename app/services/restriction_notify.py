"""
Telling people about a block.

Four messages, and the second one is the one that matters: when a
client is blocked, everybody who sells has to know before they next
speak to them. A register nobody was told about is a register people
find out about by being refused.

Delivery is `app/services/notify.py`, so these obey the same quiet
hours, the same preferences and the same internal-only recipient rule
as everything else — except the announcement itself, which is marked
urgent. "Do not do business with this client" is not a message to hold
until the morning.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _notify():
    from app.services import notify
    return notify


def _everyone_who_sells():
    """Active employees who carry work, plus every vertical head.

    Deliberately wider than the owners of the affected records: the
    point of the announcement is to stop the *next* enquiry, which will
    land with somebody who had nothing to do with this client before.
    """
    from app import Employee

    out = []
    try:
        for emp in (Employee.query
                    .filter(Employee.is_active.isnot(False))
                    .order_by(Employee.emp_code).all()):
            if not (emp.email or '').strip():
                continue
            role = (emp.role or '').lower()
            if emp.is_vertical_head or emp.is_super_admin \
                    or role in ('sales', 'presales', 'admin', 'procam_admin',
                                'user'):
                out.append(emp.emp_code)
    except Exception:
        log.exception('could not work out who to tell about a block')
    return out


def recommendation_raised(row, *, actor=None):
    """A recommendation is waiting for somebody to decide."""
    from app.services import restriction_register as register

    body = (f'{row.company_name} has been recommended for '
            f'{row.status if row.status != "recommended" else "restriction"}. '
            f'Reason: {row.reason_category}. {(row.reason_detail or "")[:300]}')
    for code in register.approvers():
        _notify().send(
            code, kind='restriction_recommended',
            event_key='restriction.recommended',
            title=f'Approval needed — {row.company_name}',
            body=body, url=f'/admin/restrictions/{row.id}',
            entity_type='ClientRestriction', entity_id=row.id,
            email=True, actor=actor,
            audit_action='restriction.recommend_notified')


def announce_block(row, closed=None):
    """Tell the company a client is blocked.

    Urgent on purpose: this is the one notification in the CRM that
    exists to stop somebody doing something, so it ignores quiet hours
    and batching.
    """
    from app.models.restriction import BLOCKED

    counts = (closed or {}).get('counts') or {}
    if row.status == BLOCKED:
        title = f'Do not do business with {row.company_name}'
        body = (f'{row.company_name} has been blocked by management. '
                f'Reason: {row.reason_category}. '
                f'{(row.reason_detail or "")[:400]} '
                f'No new lead, RFQ, quote or deal may be created for this '
                f'client. The CRM will refuse it.')
    else:
        title = f'Caution — {row.company_name}'
        body = (f'{row.company_name} is on the caution register. '
                f'Reason: {row.reason_category}. '
                f'{(row.reason_detail or "")[:400]} '
                f'Business is allowed; you will be asked to acknowledge '
                f'the warning first.')
    if counts.get('leads') or counts.get('rfqs') or counts.get('quotes'):
        body += (f' Closed with it: {counts.get("leads", 0)} lead(s), '
                 f'{counts.get("rfqs", 0)} RFQ(s), '
                 f'{counts.get("quotes", 0)} quote(s).')
    if counts.get('won'):
        body += (f' {counts["won"]} won or in-execution record(s) were '
                 f'left alone for management to decide on.')

    for code in _everyone_who_sells():
        _notify().send(
            code, kind='client_blocked', event_key='restriction.blocked',
            title=title, body=body, url=f'/admin/restrictions/{row.id}',
            entity_type='ClientRestriction', entity_id=row.id,
            email=True, urgent=True, dedupe=True)


def owners_told(row, closed):
    """The PIC of every record that was closed hears it individually.

    The company-wide announcement says a client is blocked; this says
    "and four of yours have just been closed", which is the part that
    needs a name on it.
    """
    by_owner = {}
    for kind in ('leads', 'rfqs', 'quotes'):
        for entry in (closed or {}).get(kind) or []:
            owner = (entry.get('owner') or '').strip()
            if owner:
                by_owner.setdefault(owner, []).append(entry)
    for owner, entries in by_owner.items():
        _notify().send(
            owner, kind='client_blocked_yours',
            event_key='restriction.records_closed',
            title=f'{row.company_name} blocked — {len(entries)} of your '
                  f'records closed',
            body=(f'{row.company_name} has been blocked by management '
                  f'({row.reason_category}). {len(entries)} record(s) you '
                  f'own were closed with it. They stay in the CRM and are '
                  f'no longer on your list.'),
            url=f'/admin/restrictions/{row.id}',
            entity_type='ClientRestriction', entity_id=row.id,
            email=True, urgent=True)


def review_due(row):
    """The review date has arrived — somebody said to look again."""
    target = row.approved_by or row.recommended_by
    if not target:
        return
    _notify().send(
        target, kind='restriction_review', event_key='restriction.review_due',
        title=f'Review due — {row.company_name}',
        body=(f'You set a review date of {row.review_date} on the '
              f'{row.status} for {row.company_name}. Is it still right?'),
        url=f'/admin/restrictions/{row.id}',
        entity_type='ClientRestriction', entity_id=row.id, email=True)


def daily_attempt_digest(events, recipients):
    """Who tried to do business with a restricted client yesterday."""
    if not events:
        return 0
    lines = []
    for event in events[:50]:
        payload = event.payload
        lines.append(f'{event.user_id or "?"} — {payload.get("what", "a record")}'
                     f' ({payload.get("level", "")})'
                     f' at {str(event.created_at)[:16]}')
    body = (f'{len(events)} attempt(s) to create business with a restricted '
            f'client:\n' + '\n'.join(lines))
    sent = 0
    for code in recipients:
        result = _notify().send(
            code, kind='restriction_attempts',
            event_key='restriction.attempt_digest',
            title=f'{len(events)} attempt(s) against the block register',
            body=body, url='/admin/restrictions',
            entity_type='ClientRestriction', email=True, in_app=False)
        sent += 1 if result.get('emailed') else 0
    return sent
