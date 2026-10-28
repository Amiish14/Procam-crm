"""
The morning list of owners nobody has confirmed.

A lead whose owner came from a rule rather than from knowing the
customer has a real owner from the moment it arrives — the rules
never leave a placeholder, because a lead with a placeholder owner
is a lead nothing chases. What it does not have is anybody's
agreement, and this is how that gets asked for.

One mail per head per morning, not one per lead. On last month's
volume the per-lead version would have been around 380 messages in
thirty days, nearly all of them to two people, and the second
hundred would have been deleted unread.

Unanswered after two days the lead appears on the backstop's own
digest instead. Not reassigned — a head who is on leave should come
back to their work, not to somebody else having taken it — just
raised where it will be seen.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

#: How long a head has before it is raised with the backstop.
ESCALATE_AFTER_HOURS = 48

EVENT = 'assignment.owner_confirm'
ESCALATION_EVENT = 'assignment.owner_confirm_escalation'


def _esc(value):
    from email_ingest import notifier
    return notifier._esc(value)


def pending(emp_code=None, *, older_than_hours=None):
    """Confirmations nobody has answered."""
    from app import db
    from app.models.assignment import OwnerConfirmation

    try:
        q = OwnerConfirmation.query.filter(
            OwnerConfirmation.confirmed_at.is_(None))
        if emp_code:
            q = q.filter(OwnerConfirmation.emp_code == emp_code)
        if older_than_hours:
            cutoff = datetime.utcnow() - timedelta(hours=older_than_hours)
            q = q.filter(OwnerConfirmation.created_at <= cutoff)
        return q.order_by(OwnerConfirmation.created_at).limit(500).all()
    except Exception:
        db.session.rollback()
        return []


def rows_for(confirmations):
    """What the mail shows per lead, read once rather than per row."""
    from app import Company, Lead, db

    if not confirmations:
        return []
    lead_ids = [c.lead_id for c in confirmations]
    leads = {l.id: l for l in Lead.query.filter(
        Lead.id.in_(lead_ids), Lead.junk_deleted.isnot(True)).all()}
    account_ids = {l.company_id for l in leads.values() if l.company_id}
    accounts = {}
    if account_ids:
        accounts = {c.id: c for c in
                    Company.query.filter(Company.id.in_(account_ids)).all()}

    out = []
    for c in confirmations:
        lead = leads.get(c.lead_id)
        if lead is None:
            continue
        waited = (datetime.utcnow() - c.created_at).days if c.created_at \
            else 0
        out.append({
            'lead': lead, 'confirmation': c,
            'account': accounts.get(lead.company_id),
            'waited_days': waited,
            'why': c.rule or 'assigned by rule',
        })
    return out


def body_html(emp_code, rows, *, escalation=False):
    from email_ingest import notifier

    base = notifier.base_url()
    title = ('Owners nobody has confirmed' if escalation
             else 'New clients assigned to you — confirm or reassign')
    lead_in = (
        'These were assigned by rule more than two days ago and the '
        'head has not answered. They have owners; nobody has agreed '
        'to them.' if escalation else
        'Each of these arrived from a client the CRM could not place, '
        'so it went to you as head of the vertical. They are yours '
        'now — this is asking whether they should be.')

    parts = [
        '<div style="font-family:Arial,sans-serif;font-size:14px;'
        'color:#111;max-width:680px">',
        f'<p style="font-size:17px;font-weight:600;margin:0 0 6px">'
        f'{_esc(title)}</p>',
        f'<p style="color:#6B6762;font-size:13px;margin:0 0 16px">'
        f'{_esc(lead_in)}</p>',
        '<table role="presentation" width="100%" style="border-collapse:'
        'collapse;font-size:13px">',
    ]
    for row in rows:
        lead, conf = row['lead'], row['confirmation']
        account = row['account']
        waited = row['waited_days']

        # Built as pieces rather than one long template: an f-string
        # carrying nested quotes and HTML is unreadable and, worse,
        # easy to get subtly wrong.
        company = _esc(lead.company or '(no company)')
        meta = [f'#{lead.id}',
                _esc(lead.procam_vertical or 'no vertical'),
                _esc(lead.stage or '')]
        meta.append(f'account {_esc(account.name)}' if account
                    else '<strong>no account on file</strong>')
        if waited:
            meta.append(f'waiting {waited} day'
                        + ('' if waited == 1 else 's'))
        why = _esc(row['why'])
        if escalation:
            why += f' · currently {_esc(conf.emp_code)}'

        button = ('background:%s;color:#fff;padding:7px 14px;'
                  'border-radius:6px;text-decoration:none;'
                  'font-weight:600;font-size:12.5px;margin-right:8px')
        parts.append(
            '<tr><td style="padding:10px 0;border-top:1px solid #EFEDEB">'
            f'<div style="font-weight:600">{company}</div>'
            '<div style="color:#6B6762;font-size:12.5px;margin:2px 0 6px">'
            + ' · '.join(x for x in meta if x) +
            '</div>'
            '<div style="color:#6B6762;font-size:12px;margin-bottom:8px">'
            f'{why}</div>'
            f'<a href="{base}/owner-confirm?lead={lead.id}&do=confirm" '
            f'style="{button % "#BC1D2F"}">Yes, mine</a>'
            f'<a href="{base}/owner-confirm?lead={lead.id}" '
            f'style="{button % "#6B7280"}">Give it to somebody else</a>'
            f'<a href="{base}/app?lead={lead.id}" '
            'style="color:#BC1D2F;font-size:12.5px">Open the lead</a>'
            '</td></tr>')
    parts.append('</table>')
    tail = ('.' if escalation else
            f', and after {ESCALATE_AFTER_HOURS} hours they are raised '
            f'with the CRM administrator.')
    parts.append(
        '<p style="color:#6B6762;font-size:12.5px;margin-top:18px">'
        f'All of them at once: <a href="{base}/owner-confirm">'
        f'{base}/owner-confirm</a>. Leaving them is fine — they have '
        f'owners either way{tail}</p></div>')
    return ''.join(parts)


def send_to(emp_code, *, escalation=False, commit=True):
    """Queue one person's digest. Returns the row, or None."""
    from app import Employee
    from app.services import outbox

    confirmations = (pending(older_than_hours=ESCALATE_AFTER_HOURS)
                     if escalation else pending(emp_code))
    if escalation:
        # The backstop sees everything stale, whoever holds it.
        confirmations = [c for c in confirmations]
    if not confirmations:
        return None

    rows = rows_for(confirmations)
    if not rows:
        return None

    emp = Employee.query.filter_by(emp_code=emp_code).first()
    address = (getattr(emp, 'email', '') or '').strip()
    if not address:
        log.info('no address for %s — cannot send the owner digest',
                 emp_code)
        return None

    subject = (f'{len(rows)} lead(s) waiting for an owner to be confirmed'
               if escalation else
               f'{len(rows)} new client(s) assigned to you — confirm or '
               f'reassign')
    row = outbox.enqueue(
        to=address, subject=subject[:400],
        html=body_html(emp_code, rows, escalation=escalation),
        dedupe_key=(f'owner-digest:{"esc" if escalation else emp_code}:'
                    f'{datetime.utcnow():%Y-%m-%d}'),
        event_key=(ESCALATION_EVENT if escalation else EVENT),
        user_code=emp_code, urgent=False, commit=commit)

    if row is not None and not escalation:
        _mark_notified(confirmations, commit=commit)
    if row is not None and escalation:
        _mark_escalated(confirmations, commit=commit)
    return row


def _mark_notified(confirmations, *, commit):
    from app import db
    now = datetime.utcnow()
    for c in confirmations:
        c.notified_at = now
    if commit:
        db.session.commit()


def _mark_escalated(confirmations, *, commit):
    from app import db
    now = datetime.utcnow()
    for c in confirmations:
        if c.escalated_at is None:
            c.escalated_at = now
    if commit:
        db.session.commit()


def run(*, commit=True):
    """One pass: a digest per head, then the escalation.

    Returns a small report. Never raises — a digest that cannot be
    built must not stop the rest of the morning's mail.
    """
    from app.services import auto_assign, flags

    report = {'sent': 0, 'people': [], 'escalated': 0, 'skipped': ''}
    if not flags.on('FEATURE_AUTO_ASSIGN'):
        report['skipped'] = 'FEATURE_AUTO_ASSIGN is off'
        return report

    everyone = {}
    for c in pending():
        everyone.setdefault(c.emp_code, []).append(c)

    for emp_code in sorted(everyone):
        try:
            if send_to(emp_code, commit=commit) is not None:
                report['sent'] += 1
                report['people'].append(
                    f'{emp_code} ({len(everyone[emp_code])})')
        except Exception:
            log.exception('could not send the owner digest to %s', emp_code)

    try:
        stale = pending(older_than_hours=ESCALATE_AFTER_HOURS)
        if stale:
            if send_to(auto_assign.backstop(), escalation=True,
                       commit=commit) is not None:
                report['escalated'] = len(stale)
    except Exception:
        log.exception('could not send the owner-confirmation escalation')
    return report
