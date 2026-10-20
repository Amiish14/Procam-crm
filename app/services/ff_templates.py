"""
The six freight-forwarding emails, in the database.

FF-COMMS-01 R3. FF-ENQ, FF-RFQ, FF-QUOTE, FF-REMIND, FF-CONFIRM and
FF-BD. Seeded with wording that works and editable in Admin, because
the words belong to the business: a sentence that reads wrong to a
carrier should be fixable by whoever noticed, the day they noticed.

Rendering is deliberately dumb — `str.format_map` over a dict with a
default, so a placeholder nobody filled renders blank instead of
raising. A template is content; content that can crash the send path
is a liability, and an email with one empty line beats no email.
"""
from __future__ import annotations

from app import db
from app.models.ff import (AUDIENCE_CUSTOMER, AUDIENCE_INTERNAL,
                           AUDIENCE_VENDOR, FfEmailTemplate)

ENQ, RFQ, QUOTE = 'FF-ENQ', 'FF-RFQ', 'FF-QUOTE'
REMIND, CONFIRM, BD = 'FF-REMIND', 'FF-CONFIRM', 'FF-BD'
CODES = (ENQ, RFQ, QUOTE, REMIND, CONFIRM, BD)


class _Blank(dict):
    """A placeholder nobody supplied renders empty, not as an error."""

    def __missing__(self, key):
        return ''


SEED = (
    (ENQ, 'Enquiry acknowledgement', AUDIENCE_CUSTOMER,
     'Your enquiry {crn} — {subject}',
     '<p>Dear {contact},</p>'
     '<p>Thank you for your enquiry. We have logged it as '
     '<b>{crn}</b> and are working on it.</p>'
     '<p>{summary}</p>'
     '<p>We will come back to you by {due}.</p>'
     '<p>{signature}</p>',
     ('crn', 'subject', 'contact', 'summary', 'due', 'signature')),

    (RFQ, 'Rate request to a carrier or agent', AUDIENCE_VENDOR,
     'Rate request {reference} — {service}: {origin} to {destination}',
     '<p>Dear {vendor_name},</p>'
     '<p>Please quote your best rate for the following. Quote '
     'against reference <b>{reference}</b> and keep it in the '
     'subject line when you reply.</p>'
     '<table>'
     '<tr><td>Service</td><td>{service}</td></tr>'
     '<tr><td>Origin</td><td>{origin}</td></tr>'
     '<tr><td>Destination</td><td>{destination}</td></tr>'
     '<tr><td>Cargo</td><td>{cargo}</td></tr>'
     '<tr><td>Required by</td><td>{required_by}</td></tr>'
     '</table>'
     '<p>{note}</p>'
     '<p>Please include transit time and validity. We need your '
     'reply by {due_by}.</p>'
     '<p>{signature}</p>',
     ('reference', 'vendor_name', 'service', 'origin', 'destination',
      'cargo', 'required_by', 'note', 'due_by', 'signature')),

    (QUOTE, 'Quotation to the customer', AUDIENCE_CUSTOMER,
     'Quotation {crn} — {subject}',
     '<p>Dear {contact},</p>'
     '<p>Our quotation against {crn} is below, valid until '
     '{validity}.</p>'
     '{lines_table}'
     '<p><b>Total: {total}</b></p>'
     '<p>Terms: {terms}</p>'
     '<p>{signature}</p>',
     ('crn', 'subject', 'contact', 'validity', 'lines_table', 'total',
      'terms', 'signature')),

    (REMIND, 'Reminder: rate still awaited', AUDIENCE_VENDOR,
     'Reminder — rate request {reference} ({service})',
     '<p>Dear {vendor_name},</p>'
     '<p>We asked for a rate against <b>{reference}</b> on {asked_on} '
     'and have not had it yet. If you are not quoting this one, a '
     'one-line reply saying so helps us as much as a rate.</p>'
     '<p>{signature}</p>',
     ('reference', 'vendor_name', 'service', 'asked_on', 'signature')),

    (CONFIRM, 'Order confirmation to the customer', AUDIENCE_CUSTOMER,
     'Confirmed — {crn} {subject}',
     '<p>Dear {contact},</p>'
     '<p>Thank you for your confirmation against {crn}. We are '
     'proceeding and will send the operational details '
     'separately.</p>'
     '<p>{summary}</p>'
     '<p>{signature}</p>',
     ('crn', 'subject', 'contact', 'summary', 'signature')),

    (BD, 'Business development introduction', AUDIENCE_CUSTOMER,
     '{subject}',
     '<p>Dear {contact},</p>'
     '<p>{body}</p>'
     '<p>{signature}</p>',
     ('subject', 'contact', 'body', 'signature')),
)


def seed(actor='system'):
    """Create any template that is missing. Never overwrites.

    An administrator's edit must survive a redeploy — a seeder that
    resets the wording on every boot is how somebody's careful
    rewrite disappears overnight.
    """
    made = []
    for code, name, audience, subject, html, fields in SEED:
        if FfEmailTemplate.query.filter_by(code=code).first():
            continue
        db.session.add(FfEmailTemplate(
            code=code, name=name, audience=audience, subject=subject,
            body_html=html, placeholders=list(fields),
            is_active=True, updated_by=actor))
        made.append(code)
    db.session.flush()
    return made


def get(code):
    return FfEmailTemplate.query.filter_by(code=code,
                                           is_active=True).first()


def render(code, values):
    """(subject, html) with the values filled in.

    Raises if the template is missing, because a send path that
    silently substitutes a default would deliver a message nobody
    wrote.
    """
    tpl = get(code)
    if tpl is None:
        raise LookupError(
            f'No active template {code!r}. Seed the templates or '
            f'enable it in Admin before sending.')
    safe = _Blank(values or {})
    return (str(tpl.subject).format_map(safe),
            str(tpl.body_html).format_map(safe))


def is_vendor_facing(code):
    tpl = get(code)
    return tpl is not None and tpl.audience == AUDIENCE_VENDOR
