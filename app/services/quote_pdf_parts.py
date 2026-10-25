"""
The parts of a quotation PDF that make it a company's document.

CRM-QUOTE-REV-02 §5.5, behind `FEATURE_QUOTE_REV_V2`.

Today's PDF opens with "Quote QUO-2026-0001" and a line reading
`Status: Draft (rev 1)`. That status line is for us and has been
going to customers; a quotation that announces itself as a draft
is not one anybody acts on.

Kept in its own module so the PDF route stays readable and so
every piece can be tested without rendering a document.
"""
from __future__ import annotations

FLAG = 'FEATURE_QUOTE_REV_V2'

CONFIDENTIALITY = (
    'This quotation and its contents are confidential and are '
    'intended only for the addressee. Rates are subject to the '
    'conditions stated above and to space and equipment being '
    'available at the time of booking.')


def enabled(emp_code=None):
    from app.services import quote_rev
    return quote_rev.enabled(emp_code)


def display_number(quote):
    from app.services import quote_rev
    return quote_rev.display_number(quote)


def business_unit(quote):
    """The BU this quotation goes out under, as a dict.

    From the BU master where there is one. Falls back to the
    company's own name and nothing else: a letterhead with an
    invented address is worse than a plain one.
    """
    default = {'name': 'Procam Logistics', 'address': '',
               'gstin': '', 'signatory': 'Authorised Signatory'}
    try:
        from app.services import admin_settings
        found = admin_settings.get('business_unit') or {}
        if isinstance(found, dict) and found.get('name'):
            return {**default, **found}
    except Exception:                                     # noqa: BLE001
        pass
    return default


def letterhead(quote, styles):
    """Who is sending this, at the top of page one."""
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer

    bu = business_unit(quote)
    head = ParagraphStyle('bu', parent=styles['Normal'], fontSize=10,
                          leading=13, spaceAfter=0)
    bits = [f'<b>{_esc(bu["name"])}</b>']
    if bu.get('address'):
        bits.append(_esc(bu['address']))
    if bu.get('gstin'):
        bits.append(f'GSTIN: {_esc(bu["gstin"])}')
    return [Paragraph('<br/>'.join(bits), head), Spacer(1, 5)]


def customer_block(quote, body):
    """Who it is addressed to, and what it answers."""
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer

    lines = []
    name, address = _customer(quote)
    if name:
        lines.append(f'<b>To:</b> {_esc(name)}')
    if address:
        lines.append(_esc(address))
    ref = our_ref(quote)
    if ref:
        lines.append(f'<b>Our ref:</b> {_esc(ref)}')
    if not lines:
        return []
    return [Paragraph('<br/>'.join(lines), body), Spacer(1, 3 * mm)]


def _customer(quote):
    from app import Company, db

    if not quote.account_id:
        return '', ''
    account = db.session.get(Company, quote.account_id)
    if account is None:
        return '', ''
    return (account.name or ''), (account.address or '')


def our_ref(quote):
    """"RFQ-2026-0007, Lead #10421" — what this answers."""
    from app import db

    bits = []
    if quote.rfq_id:
        from app.models.rfq import RFQ
        rfq = db.session.get(RFQ, quote.rfq_id)
        if rfq is not None and rfq.rfq_number:
            bits.append(rfq.rfq_number)
    if quote.lead_id:
        bits.append(f'Lead #{quote.lead_id}')
    return ', '.join(bits)


def footer(quote, body, styles):
    """Confidentiality, then who signs it."""
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer

    bu = business_unit(quote)
    small = ParagraphStyle('conf', parent=styles['Normal'],
                           fontSize=7.5, leading=10,
                           textColor='#555555')
    return [
        Spacer(1, 6 * mm),
        Paragraph(CONFIDENTIALITY, small),
        Spacer(1, 8 * mm),
        Paragraph(f'For <b>{_esc(bu["name"])}</b>', body),
        Spacer(1, 10 * mm),
        Paragraph(_esc(bu.get('signatory')
                       or 'Authorised Signatory'), body),
    ]


def _esc(text):
    return (str(text or '').replace('&', '&amp;')
            .replace('<', '&lt;').replace('>', '&gt;'))
