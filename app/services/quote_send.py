"""
Sending a quote, and asking a vendor for a rate, through the mailer
that already exists.

CRM 2.0 R5. Nothing here opens an SMTP connection or talks to Graph:
both paths hand the message to `outbox.enqueue`, which is where the
caps, the retries, the dedupe, the kill switch and the audit already
live. `mailer.transport()` decides SMTP or Graph, so "Outlook send"
becomes a configuration change rather than a second send path.

The caps are not re-implemented either
    20 a minute, 3,000 recipients a day, 100 a day for business
    development. `outbox.day_room()` and `throttle_room()` answer
    those, and the worker holds anything over. What this module adds
    is refusing *before* queueing when the day is already full, so a
    person pressing Send is told now rather than finding out
    tomorrow that it sat in a queue.

Masking is not optional on a vendor mail
    A forwarding rate request must not tell a carrier whose cargo it
    is. `ff_privacy.assert_clean` raises rather than redacting at
    this point: reaching the send path with the client's name still
    in the body means the redaction missed something, and quietly
    patching it over would hide the gap.

    Outside forwarding the masking is off by default, because the
    rule Nilesh set was that FF keeps its masking rules — a crane
    hire enquiry naming the site is normal. `mask=True` turns it on
    for any vertical if that changes.
"""
from __future__ import annotations

from app.services import flags, outbox

#: Event keys, so notification preferences and the business-
#: development cap can tell these apart from everything else.
EVENT_QUOTE = 'quote.sent'
EVENT_RATE = 'vendor.rate_request'


class SendRefused(RuntimeError):
    """Not queued, and the caller should say why to the person."""


def _room_or_refuse(n=1):
    room = outbox.day_room()
    if room is not None and room < n:
        raise SendRefused(
            f'The daily recipient cap is reached — {room} left and '
            f'{n} needed. Nothing was queued; try tomorrow or ask an '
            f'administrator to raise the cap.')
    return room


def send_quote(quote, *, to, actor=None, subject=None, note=None,
               attach_pdf_path=None, cc=None):
    """Queue a quote to the customer. Returns the outbox row or None.

    The terms are stamped before it goes: a quote emailed without a
    version recorded is a quote nobody can later prove the wording
    of.
    """
    from app.services import quote_build, quote_terms

    recipients = [to] if isinstance(to, str) else list(to or [])
    if not recipients:
        raise SendRefused('Name somebody to send it to.')
    _room_or_refuse(len(recipients))

    quote_build.recalculate(quote, actor=actor)
    printed = quote_terms.for_print(quote)

    subject = subject or (
        f'Quotation {quote.quote_number}' if quote.quote_number
        else f'Quotation for your enquiry')
    html = _quote_html(quote, printed, note)
    return outbox.enqueue(
        to=recipients, cc=cc, subject=subject, html=html,
        dedupe_key=outbox.make_key(EVENT_QUOTE, quote.id,
                                   quote.total_amount,
                                   printed['standard']['version'],
                                   *sorted(recipients)),
        event_key=EVENT_QUOTE, user_code=actor,
        entity_type='quote', entity_id=quote.id,
        attachments=([attach_pdf_path] if attach_pdf_path else None))


def send_rate_request(req, *, to=None, actor=None, line=None,
                      rfq=None, account=None, lead=None, mask=None,
                      subject=None):
    """Queue one vendor's rate request, with the client taken out.

    One mail per vendor, never a shared thread: two carriers on one
    To: line learn they are bidding against each other and both
    learn who the client is.
    """
    from app.services import ff_privacy

    to = to or (req.vendor_email or '').strip()
    if not to:
        raise SendRefused(
            f'{req.vendor_name} has no email address on file.')
    _room_or_refuse(1)

    if mask is None:
        mask = _masks_by_default(lead, rfq)

    subject = subject or _rate_subject(req, rfq)
    html = _rate_html(req, line, rfq)

    if mask:
        secrets = ff_privacy.secrets_for(
            account=account, lead=lead,
            keep=[getattr(line, 'origin', None),
                  getattr(line, 'destination', None)])
        subject = ff_privacy.redact(subject, *secrets)
        html = ff_privacy.redact(html, *secrets)
        # Raises rather than redacting again: getting here with the
        # client still named means something upstream missed it.
        ff_privacy.assert_clean(
            [subject, html], secrets.names, secrets.domains,
            secrets.phones,
            what=f'the rate request to {req.vendor_name}')

    return outbox.enqueue(
        to=[to], subject=subject, html=html,
        dedupe_key=outbox.make_key(EVENT_RATE, req.id, to),
        event_key=EVENT_RATE, user_code=actor,
        entity_type='rate_request', entity_id=req.id)


def _masks_by_default(lead, rfq):
    """Forwarding masks; the rest do not, until somebody says so."""
    from app.services import ff_charges

    vertical = (getattr(lead, 'procam_vertical', None) or '').strip()
    return ff_charges.uses_incoterms(vertical)


def _rate_subject(req, rfq):
    ref = getattr(rfq, 'vendor_crn', None) or ''
    return f'Rate request {ref}'.strip()


def _rate_html(req, line, rfq):
    bits = ['<p>Please quote for the following:</p>', '<ul>']
    for label, value in (
            ('Service', getattr(line, 'service', '')),
            ('Scope', getattr(line, 'scope', '')),
            ('From', getattr(line, 'origin', '')),
            ('To', getattr(line, 'destination', '')),
            ('Cargo', getattr(line, 'cargo', '')),
            ('Required by', getattr(line, 'required_by', ''))):
        if value:
            bits.append(f'<li><b>{label}:</b> {value}</li>')
    bits.append('</ul>')
    if req.due_by:
        bits.append(f'<p>Please reply by {req.due_by}.</p>')
    if req.request_note:
        bits.append(f'<p>{req.request_note}</p>')
    return '\n'.join(bits)


def _quote_html(quote, printed, note):
    from app.models.quote import QuoteLine

    lines = (QuoteLine.query.filter_by(quote_id=quote.id)
             .order_by(QuoteLine.line_no.asc()).all())
    rows = ''.join(
        f'<tr><td>{l.line_no}</td><td>{l.service or ""}</td>'
        f'<td>{l.description or ""}</td>'
        f'<td align="right">{float(l.line_total or 0):,.2f}</td></tr>'
        for l in lines)
    bits = [f'<p>{note}</p>' if note else '',
            '<table border="1" cellpadding="6" cellspacing="0">',
            '<tr><th>#</th><th>Service</th><th>Description</th>'
            '<th>Amount</th></tr>', rows,
            f'<tr><td colspan="3" align="right"><b>Total</b></td>'
            f'<td align="right"><b>'
            f'{float(quote.total_amount or 0):,.2f} '
            f'{quote.currency or "INR"}</b></td></tr>',
            '</table>']
    if printed['special']:
        bits.append('<p><b>Special conditions</b></p><ol>')
        bits += [f'<li>{c["text"]}</li>' for c in printed['special']]
        bits.append('</ol>')
    std = printed['standard']
    if std['missing']:
        bits.append('<p><i>No standard terms are published for '
                    f'{printed["vertical"]}.</i></p>')
    else:
        bits.append(f'<p><b>{std["title"]}</b> (v{std["version"]})</p>')
        bits.append('<p>' + (std['body'] or '').replace('\n', '<br>')
                    + '</p>')
    return '\n'.join(b for b in bits if b)


def external_mail_allowed():
    """Whether a client or vendor address can be reached at all.

    `mail_policy` refuses every non-Procam address unless
    CRM_EMAIL_ALLOW_EXTERNAL is set. A screen offering Send while
    that is off would queue nothing and say it had sent, so callers
    check this and say so instead.
    """
    from app.services import mail_policy
    return mail_policy.external_allowed()
