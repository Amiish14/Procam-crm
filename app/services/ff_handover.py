"""
The confirmation is the purchase order; the pack is what operations
gets.

FF-COMMS-01 R4. On a forwarding job the customer rarely issues a PO
document — they reply "go ahead" to the quotation, and that email is
the instruction. So it is captured as evidence: sender, time,
subject and Message-Id. "The client confirmed" without those is
hearsay, and it is exactly what gets argued about when a shipment
goes wrong three weeks later.

What this does not do
    It does not call the TMS. The payload is built and stored so it
    can be read and approved before anything is wired to a live
    system, and `notify_tms` is deliberately not imported here. A
    release that quietly started posting to production because a
    flag flipped would be the worst possible way to find a mistake
    in the format.

A caution about the format
    FF-COMMS-02 is not in this repository and neither is any sample
    of the Excel pack the TMS accepts. The structure below is
    derived from what the CRM holds and from the shape of the two
    payloads the TMS is already sent (`lead.won`, `quote.won`):
    flat, `crm_`-prefixed identifiers. Every field is listed in
    `INVENTED` so the differences from the real format can be found
    and corrected in one place rather than hunted through the code.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from decimal import Decimal

from app import db

#: Fields this release invented because the real pack format was not
#: available. Checked by a test, so the list cannot rot.
INVENTED = (
    'shipment.mode', 'shipment.incoterm', 'shipment.client_party',
    'shipment.chargeable', 'charges[].charge_group',
    'charges[].cost_inr', 'charges[].vendor', 'confirmation.message_id',
    'totals.margin_inr',
)

PACK_DIR = os.path.join('docs', 'exports', 'ff_handover')


class HandoverError(ValueError):
    """The job is not in a state that can be handed over."""


def capture_confirmation(quote, *, email, subject, message_id=None,
                         note=None, when=None, actor=None):
    """Record the customer's go-ahead against the quote.

    Refuses a confirmation with no sender: an instruction nobody can
    be shown to have given is not evidence of anything, and the
    whole point of capturing it is that it can be produced later.
    """
    from app.models.ff import FfHandoverPack

    addr = (email or '').strip()
    if '@' not in addr:
        raise HandoverError(
            'A confirmation needs the address it came from. Without '
            'it there is nothing to show when somebody asks who '
            'authorised the shipment.')
    if quote.status in ('Lost', 'Withdrawn', 'Superseded'):
        raise HandoverError(
            f'This quote is {quote.status}; a confirmation against it '
            f'would be filed on a version nobody is working.')

    pack = FfHandoverPack.query.filter_by(quote_id=quote.id).first()
    if pack is None:
        pack = FfHandoverPack(quote_id=quote.id, crn=getattr(quote, 'crn', None))
        db.session.add(pack)
    pack.confirmed_at = when or datetime.utcnow()
    pack.confirmed_by_email = addr[:320]
    pack.confirmation_subject = (subject or '')[:400]
    pack.confirmation_message_id = (message_id or '')[:400] or None
    pack.confirmation_note = note
    pack.status = 'Draft'
    db.session.flush()
    return pack


def mark_won(quote, *, actor=None):
    """Won, on the strength of a captured confirmation.

    The confirmation has to exist first. Marking a forwarding job won
    without it is the same gap the CRM already has elsewhere — 168
    won deals with no value and no evidence — and this is the one
    place it can be closed at the point of entry.
    """
    from app.models.ff import FfHandoverPack

    pack = FfHandoverPack.query.filter_by(quote_id=quote.id).first()
    if pack is None or not pack.confirmed_at:
        raise HandoverError(
            'Capture the customer\'s confirmation first. On a '
            'forwarding job that email is the purchase order, and '
            'winning without it leaves nothing to show.')
    quote.status = 'Won'
    quote.won_at = datetime.utcnow()
    pack.status = 'Ready'
    return pack


def build_payload(quote, *, pack=None, lead=None, account=None,
                  rfq=None):
    """The TMS payload, built and returned. Never sent."""
    from app.models.ff import FfHandoverPack, RateRequest
    from app.models.quote import QuoteLine
    from app.services import ff_rates

    pack = pack or FfHandoverPack.query.filter_by(
        quote_id=quote.id).first()
    lines = (QuoteLine.query.filter_by(quote_id=quote.id)
             .order_by(QuoteLine.line_no.asc()).all())

    charges = []
    for ln in lines:
        vendor = None
        if ln.source_rate_request_id:
            rr = db.session.get(RateRequest, ln.source_rate_request_id)
            vendor = rr.vendor_name if rr else None
        charges.append({
            'line_no': ln.line_no,
            'service': ln.service or '',
            'description': ln.description or '',
            'charge_group': ln.charge_group or 'other',
            'sell_inr': float(ln.line_total or 0),
            'cost_inr': (float(ln.cost_inr)
                         if ln.cost_inr is not None else None),
            'vendor': vendor,
        })

    margin = ff_rates.margin_summary(quote)
    return {
        'event': 'ff.handover',
        'crm_quote_id': quote.id,
        'crm_lead_id': getattr(quote, 'lead_id', None),
        'crm_account_id': getattr(quote, 'account_id', None),
        'crn': getattr(quote, 'crn', '') or '',
        'quote_number': getattr(quote, 'quote_number', '') or '',
        'shipment': {
            'mode': getattr(lead, 'mode', None) or '',
            'incoterm': getattr(quote, 'incoterm', None) or '',
            'client_party': getattr(quote, 'client_party', None) or '',
            'origin': getattr(rfq, 'origin', None) or '',
            'destination': getattr(rfq, 'destination', None) or '',
            'cargo': getattr(rfq, 'cargo', None) or '',
        },
        'customer': {
            'name': (getattr(account, 'name', None)
                     or getattr(lead, 'company', None) or ''),
            'crm_account_id': getattr(account, 'id', None),
        },
        'confirmation': {
            'received_at': (str(pack.confirmed_at)[:19]
                            if pack and pack.confirmed_at else ''),
            'from': (pack.confirmed_by_email if pack else ''),
            'subject': (pack.confirmation_subject if pack else ''),
            'message_id': (pack.confirmation_message_id if pack else ''),
        },
        'charges': charges,
        'totals': {
            'sell_inr': margin['sell_inr'],
            'cost_inr': margin['cost_inr'],
            'margin_inr': margin['margin_inr'],
            'currency': getattr(quote, 'currency', None) or 'INR',
            'margin_reliable': margin['reliable'],
        },
        'built_at': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
        'format_note': ('Derived from CRM data; FF-COMMS-02 was not '
                        'available. Invented fields are listed in '
                        'app/services/ff_handover.INVENTED.'),
    }


def build_pack(quote, *, actor=None, lead=None, account=None, rfq=None,
               out_dir=None):
    """Write the Excel pack and store the payload beside it."""
    from app.models.ff import FfHandoverPack

    pack = FfHandoverPack.query.filter_by(quote_id=quote.id).first()
    if pack is None or not pack.confirmed_at:
        raise HandoverError(
            'There is no captured confirmation for this quote, so '
            'there is nothing to hand over.')

    payload = build_payload(quote, pack=pack, lead=lead,
                            account=account, rfq=rfq)
    path = _write_xlsx(payload, quote, out_dir=out_dir)
    pack.payload_json = json.dumps(payload, indent=2, default=str)
    pack.file_path = path
    pack.built_at = datetime.utcnow()
    pack.built_by = actor
    pack.status = 'Ready'
    return pack, payload, path


def _write_xlsx(payload, quote, *, out_dir=None):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    root = out_dir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), PACK_DIR)
    os.makedirs(root, exist_ok=True)
    name = (payload.get('crn') or payload.get('quote_number')
            or f'quote-{quote.id}')
    # Two payload shapes share this writer: a forwarding handover
    # carries a `shipment` block, a job handover carries `job`. The
    # charges sheet is identical for both, which is the half worth
    # not duplicating.
    detail = payload.get('shipment') or payload.get('job') or {}
    is_job = 'job' in payload
    prefix = 'job_handover' if is_job else 'ff_handover'
    path = os.path.join(root, f'{prefix}_{name}.xlsx')

    wb = Workbook()
    ws = wb.active
    ws.title = 'Handover'
    head = Font(bold=True, color='FFFFFF')
    fill = PatternFill('solid', fgColor='1F2937')

    ws.append([(f'{detail.get("vertical") or "Job"} handover pack')
               if is_job else 'Freight Forwarding handover pack'])
    ws['A1'].font = Font(bold=True, size=14)
    ws.append([payload['format_note']])
    ws.append([])
    if is_job:
        specific = (
            ('Vertical', detail.get('vertical', '')),
            ('Job type', detail.get('job_type', '')),
            ('From', detail.get('site_from', '')),
            ('To', detail.get('site_to', '')),
            ('Scope', detail.get('scope', '')),
            ('Cargo', detail.get('cargo', '')),
            ('Start by', detail.get('start_by', '')),
        )
    else:
        specific = (
            ('Mode', detail.get('mode', '')),
            ('Incoterm', detail.get('incoterm', '')),
            ('Client is', detail.get('client_party', '')),
            ('Origin', detail.get('origin', '')),
            ('Destination', detail.get('destination', '')),
            ('Cargo', detail.get('cargo', '')),
        )
    for label, value in (
            ('CRN', payload['crn']),
            ('Quote', payload['quote_number']),
            ('Customer', payload['customer']['name']),
    ) + specific + (
            ('Confirmed at', payload['confirmation']['received_at']),
            ('Confirmed by', payload['confirmation']['from']),
            ('Confirmation subject', payload['confirmation']['subject']),
            ('Message-Id', payload['confirmation']['message_id'])):
        ws.append([label, value])
        ws.cell(ws.max_row, 1).font = Font(bold=True)
    ws.column_dimensions['A'].width = 24
    ws.column_dimensions['B'].width = 60

    cs = wb.create_sheet('Charges')
    cs.append(['Line', 'Service', 'Charge group', 'Description',
               'Sell (INR)', 'Cost (INR)', 'Margin (INR)', 'Vendor'])
    for cell in cs[1]:
        cell.font, cell.fill = head, fill
    for c in payload['charges']:
        sell = c['sell_inr'] or 0
        cost = c['cost_inr']
        cs.append([c['line_no'], c['service'], c['charge_group'],
                   c['description'], sell, cost,
                   (sell - cost) if cost is not None else '',
                   c['vendor'] or ''])
    t = payload['totals']
    cs.append([])
    cs.append(['', 'TOTAL', '', '', t['sell_inr'], t['cost_inr'],
               t['margin_inr'], ''])
    cs.cell(cs.max_row, 2).font = Font(bold=True)
    if not t['margin_reliable']:
        cs.append([])
        cs.append(['', 'Some lines carry no cost — the margin above is '
                       'overstated by whatever they cost.'])
        cs.cell(cs.max_row, 2).font = Font(bold=True, color='B45309')
    for i, w in enumerate((7, 26, 18, 40, 14, 14, 14, 24), start=1):
        cs.column_dimensions[cs.cell(1, i).column_letter].width = w
    cs.freeze_panes = 'A2'

    wb.save(path)
    return path
