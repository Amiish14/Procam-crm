"""
Asking the customer for what the RFQ did not say.

CRM-RFQ-CHAIN-01 §4.5.

Screening finds something missing — drawings, the site address,
the scope, the required date — and the step stays open while the
customer is chased. Until now it stayed open and nobody was
chased: the CRM recorded "waiting for customer" and sent nothing.

One honest limitation, surfaced rather than hidden
    `CRM_EMAIL_ALLOW_EXTERNAL` is not set on this installation, so
    `mail_policy` refuses every address outside Procam and the
    outbox declines the row. The mail is still composed and the
    attempt is still recorded; the caller is told plainly that it
    was held rather than sent, so nobody waits a week for a reply
    to a message that never left. The day the variable is set, the
    same code sends.
"""
from __future__ import annotations

#: What each screening outcome actually asks the customer for.
#: Written out rather than echoing the dropdown back at them: "Missing:
#: drawings / dimensions" is a note to ourselves, not a question.
ASKS = {
    'Missing: drawings / dimensions':
        'the drawings or the overall dimensions and weight of the '
        'cargo',
    'Missing: site address / route':
        'the delivery site address, and any route restrictions you '
        'already know of',
    'Missing: scope of work':
        'what is in our scope — loading, lashing, unloading, '
        'installation — and what you will handle',
    'Missing: required date':
        'the date you need this by',
}


def question_for(outcome):
    return ASKS.get(outcome, 'the missing details noted against this '
                             'enquiry')


def recipient(rfq, lead):
    """(to, name) — the customer contact this goes to, or ('', '').

    The RFQ's own contact first, then the lead's, then the account's.
    Never an internal address: a clarification mailed to ourselves
    is a step that closes on nothing.
    """
    from app import Company, Contact, db
    from app.services.mail_signals import is_internal

    tries = []
    for attr in ('contact_email', 'email'):
        tries.append((getattr(rfq, attr, '') or '', ''))
    if lead is not None:
        tries.append(((lead.email or ''), (lead.pic or '')))
        tries.append(((lead.email2 or ''), (lead.pic or '')))
    account_id = getattr(rfq, 'account_id', None) \
        or getattr(lead, 'company_id', None)
    if account_id:
        contact = (Contact.query
                   .filter(db.or_(Contact.account_id == account_id,
                                  Contact.company_id == account_id))
                   .order_by(Contact.id.asc()).first())
        if contact is not None:
            tries.append(((contact.email or ''),
                          (contact.name or '')))
        account = db.session.get(Company, account_id)
        if account is not None:
            tries.append(((account.email or ''), ''))

    for address, name in tries:
        address = (address or '').strip()
        if address and '@' in address and not is_internal(address):
            return address, name
    return '', ''


def compose(rfq, lead, outcome, *, extra=''):
    """(subject, html). Plain, short, and it asks one thing."""
    reference = (rfq.rfq_number or f'enquiry #{getattr(lead, "id", "")}')
    subject = f'{reference}: one thing we need before we can quote'
    asked = question_for(outcome)
    body = [
        f'<p>Thank you for the enquiry'
        f'{" — " + rfq.subject if rfq.subject else ""}.</p>',
        f'<p>Before we can price it accurately we need '
        f'<b>{asked}</b>.</p>',
    ]
    if extra.strip():
        body.append(f'<p>{_esc(extra.strip())}</p>')
    body.append('<p>Once we have that we will come back with our '
                'quotation. Our reference is '
                f'<b>{_esc(reference)}</b> — please keep it on the '
                'reply so it reaches the right desk.</p>')
    return subject, '\n'.join(body)


def _esc(text):
    return (str(text or '').replace('&', '&amp;')
            .replace('<', '&lt;').replace('>', '&gt;'))


def send(rfq, lead, outcome, *, actor, extra='', commit=False):
    """Queue the clarification. (status, detail).

    status is 'queued', 'held' or 'no-recipient'. 'held' means the
    mail was composed and the outbox declined it because external
    mail is switched off — the step still waits for the customer,
    but somebody has to send it by hand today.
    """
    from app import db
    from app.services import audit, mail_policy, outbox
    from email_ingest.service import DEFAULT_INBOX

    to, name = recipient(rfq, lead)
    if not to:
        audit.record('rfq.clarification_not_sent', 'rfq', rfq.id,
                     new={'outcome': outcome}, actor=actor,
                     reason='no customer address on the RFQ, the '
                            'lead or the account')
        return 'no-recipient', ('No customer address on this RFQ, '
                                'the lead or the account — ask them '
                                'by hand and note it here.')

    subject, html = compose(rfq, lead, outcome, extra=extra)
    allowed, refused = mail_policy.check([to])
    row = outbox.enqueue(
        to=to, cc=[DEFAULT_INBOX],
        subject=subject, html=html,
        dedupe_key=outbox.make_key('rfq.clarify', rfq.id, outcome),
        event_key='rfq.clarification', user_code=actor,
        entity_type='rfq', entity_id=rfq.id, commit=commit)

    status = 'queued' if row is not None else 'held'
    audit.record('rfq.clarification_sent' if row is not None
                 else 'rfq.clarification_held',
                 'rfq', rfq.id,
                 new={'to': to, 'cc': DEFAULT_INBOX,
                      'outcome': outcome, 'subject': subject},
                 actor=actor,
                 reason=(f'asked the customer for '
                         f'{question_for(outcome)}'
                         if row is not None else
                         'composed but not sent — external mail is '
                         'switched off on this installation'))
    if commit:
        db.session.commit()

    if row is not None:
        return 'queued', {'to': to, 'name': name, 'subject': subject}
    return 'held', {
        'to': to, 'subject': subject,
        'why': ('External email is switched off here '
                '(CRM_EMAIL_ALLOW_EXTERNAL), so this was not sent. '
                'The step stays open — send it by hand today, or '
                'ask an admin to turn external mail on.')
        if refused else
        'The outbox declined it; check Email Health.'}
