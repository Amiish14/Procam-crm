"""
Telling the person who forwarded a mail what became of it.

Sanjeev forwarded two enquiries and heard nothing. One was attached
to a Lost lead, the other was dropped as a reply. Both were correct
decisions by the rules in force; neither was communicated, so from
where he sat the CRM had simply eaten them.

One short mail, to the forwarder only, within the usual queue delay.
Never to the client: this says what our system did, which is none of
their business.

Behind `FEATURE_FORWARD_RECEIPT`. Off and nothing changes.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

SUBJECTS = {
    'created': 'Created',
    'attached': 'Attached',
    'reopened': 'Reopened',
    'skipped': 'Not turned into a lead',
    'error': 'Could not be processed',
}

#: What each outcome means, said once, in the forwarder's terms.
EXPLAIN = {
    'created': 'A new lead was created from this.',
    'attached': 'This was filed against a lead that already existed.',
    'reopened': 'This arrived on a closed lead, so the lead was reopened.',
    'skipped': 'The CRM did not turn this into a lead.',
    'error': 'Something went wrong while processing this.',
}


def _esc(value):
    from email_ingest import notifier
    return notifier._esc(value)


def _button(href, label, *, muted=False):
    colour = '#6B7280' if muted else '#BC1D2F'
    return (f'<a href="{_esc(href)}" style="background:{colour};color:#fff;'
            f'padding:9px 16px;border-radius:6px;text-decoration:none;'
            f'font-weight:600;font-size:13px;display:inline-block;'
            f'margin:0 8px 8px 0">{_esc(label)}</a>')


def body_html(row, lead=None):
    """The receipt, as HTML."""
    from email_ingest import notifier

    base = notifier.base_url()
    outcome = (row.outcome or '').lower()
    lines = [
        '<div style="font-family:Arial,sans-serif;font-size:14px;'
        'color:#111;max-width:560px">',
        f'<p style="font-size:16px;font-weight:600;margin:0 0 4px">'
        f'{_esc(EXPLAIN.get(outcome, "This was processed."))}</p>',
        f'<p style="color:#6B6762;font-size:13px;margin:0 0 16px">'
        f'You forwarded: <strong>{_esc(row.subject or "(no subject)")}</strong>'
        f'{" — from " + _esc(row.from_addr) if row.from_addr else ""}</p>',
    ]

    if lead is not None:
        stage = (lead.stage or '').strip() or 'no stage'
        lines.append(
            '<table role="presentation" style="border-collapse:collapse;'
            'font-size:13px;margin:0 0 16px">'
            f'<tr><td style="padding:2px 14px 2px 0;color:#6B6762">Lead</td>'
            f'<td><strong>#{lead.id}</strong> {_esc(lead.company or "")}</td></tr>'
            f'<tr><td style="padding:2px 14px 2px 0;color:#6B6762">Stage</td>'
            f'<td>{_esc(stage)}</td></tr>'
            f'<tr><td style="padding:2px 14px 2px 0;color:#6B6762">Owner</td>'
            f'<td>{_esc(lead.assigned_name or lead.assigned_to or "nobody yet")}'
            f'</td></tr></table>')
        lines.append(_button(f'{base}/app?lead={lead.id}', 'Open lead'))

    if outcome == 'skipped':
        why = (row.reason or 'No reason was recorded.').strip()
        lines.append(
            f'<p style="background:#FFFBEB;border:1px solid #FCD34D;'
            f'border-radius:8px;padding:10px 12px;font-size:13px">'
            f'<strong>Why:</strong> {_esc(why)}</p>')
        lines.append(_button(f'{base}/my-mail?msg={row.id}',
                             'Create a lead from it'))
        lines.append(_button(f'{base}/my-mail?msg={row.id}',
                             'Attach it to a lead', muted=True))

    lines.append(
        f'<p style="color:#6B6762;font-size:12.5px;margin-top:18px">'
        f'Everything you forward to leads@procamgroup.in is listed on '
        f'<a href="{_esc(base)}/my-mail">My Mail</a>, with what the CRM '
        f'did about it.</p></div>')
    return ''.join(lines)


def subject_for(row, lead=None):
    label = SUBJECTS.get((row.outcome or '').lower(), 'Processed')
    tag = f'CRM #{lead.id}' if lead is not None else 'CRM'
    return f'[{tag}] {label}: {(row.subject or "(no subject)")[:120]}'


def send_for(row, *, lead=None, commit=True):
    """Queue the receipt for one ingest row. Returns True if queued.

    Never raises: a receipt is the last thing that happens to a mail
    and must not undo the lead that was just created from it.
    """
    try:
        from app.services import flags
        if not flags.on('FEATURE_FORWARD_RECEIPT'):
            return False
        if row is None or not (row.forwarded_by_emp or '').strip():
            # Nobody internal forwarded it — it came straight from a
            # client. There is no colleague waiting to hear back.
            return False

        from app import Employee, db
        from app.services import outbox

        emp = Employee.query.filter_by(
            emp_code=row.forwarded_by_emp).first()
        address = (getattr(emp, 'email', '') or '').strip()
        if not address:
            return False

        if lead is None and row.lead_id:
            from app import Lead
            lead = db.session.get(Lead, row.lead_id)

        queued = outbox.enqueue(
            to=address,
            subject=subject_for(row, lead)[:400],
            html=body_html(row, lead),
            dedupe_key=f'forward-receipt:{row.id}',
            event_key='mail.forward_receipt',
            user_code=row.forwarded_by_emp,
            entity_type='Lead' if lead is not None else 'MailIngestLog',
            entity_id=(lead.id if lead is not None else row.id),
            # The forwarder is waiting on this one: it is the answer to
            # "did it arrive". Holding it for a batching window or
            # until the morning defeats the point.
            urgent=True,
            commit=commit)
        return queued is not None
    except Exception:                                     # noqa: BLE001
        log.exception('could not queue a forward receipt for row %s',
                      getattr(row, 'id', '?'))
        return False
