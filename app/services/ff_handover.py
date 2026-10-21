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

The format is the TMS's, not ours
    `docs/tms_import_formats.md`, Part A, copied from Procam LR at
    commit c8e6e85. The pack emits exactly the field names the
    reader knows — `app/services/tms_intake.PART_A_FIELDS` — because
    a name it does not recognise is dropped without a word, which is
    the quiet way to lose a value.

    Until that document arrived this module carried an `INVENTED`
    list of field names it had made up. The list is gone; what
    replaces it is `CANNOT_SUPPLY`, which is the opposite thing —
    fields the format defines that the CRM has no column for, named
    so a person can fill them in before importing rather than
    discovering them at the far end.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from decimal import Decimal

from app import db
from app.services import tms_intake

#: What the CRM cannot answer, and what the TMS reads and drops.
#: Kept as module names because callers and tests referred to
#: `INVENTED`; these say something true instead.
CANNOT_SUPPLY = tms_intake.CANNOT_SUPPLY
READ_AND_DROPPED = tms_intake.READ_AND_DROPPED


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
                  rfq=None, gst_treatment=None):
    """The Handover Pack, in the TMS's Part A field names.

    Flat, because the reader is flat: one field name, one value,
    whether it arrives as JSON or as column A and column B of a
    sheet. Anything the CRM cannot answer is left out rather than
    sent empty — a blank cell is "not in the pack", and for a
    required field the import refuses having created nothing, which
    is the outcome we want over a job pointing the wrong way.
    """
    from app.models.ff import FfHandoverPack
    from app.services import quote_terms

    pack = pack or FfHandoverPack.query.filter_by(
        quote_id=quote.id).first()
    lead, account, rfq = _resolve(quote, lead, account, rfq)
    vertical = quote_terms.vertical_of(quote)

    fields = {
        'reference': (getattr(quote, 'crn', None)
                      or getattr(quote, 'quote_number', None)),
        # Selects the client at the far end by name, case-insensitive,
        # against clients.name. A pack never creates a customer, so a
        # name that does not match is a refusal, not a new row.
        'customer': (getattr(account, 'name', None)
                     or getattr(lead, 'company', None)),
        'mode': tms_intake.mode_for(getattr(lead, 'mode', None)),
        'incoterm': getattr(quote, 'incoterm', None),
        'incoterm_place': getattr(quote, 'incoterm_place', None),
        'direction': getattr(rfq, 'direction', None),
        'quote_reference': getattr(quote, 'quote_number', None),
        # The customer's own number, kept beside Procam's and never
        # used as it. On a forwarding job the confirmation email is
        # usually the only instruction, so this is often absent.
        'tally_po_number': (getattr(pack, 'client_po_number', None)
                            if pack is not None else None),
        # `po_value` at the far end, and 0 when absent — so it has
        # to be the real figure. `total_amount` is only right once
        # the quote has been recalculated, and a pack built from a
        # quote nobody recalculated would hand the TMS a zero-value
        # order. The lines are the fact; the header is a cache.
        'sell_value': _sell_value(quote),
        # Read and dropped by the TMS today; sent because it is in
        # the format and because a figure without its currency is
        # the thing their own gap list warns about.
        'currency': getattr(quote, 'currency', None) or 'INR',
        # The shipment, captured on the enquiry rather than left
        # for somebody to retype at the far end.
        'shipper_name': getattr(rfq, 'shipper_name', None),
        'shipper_address': getattr(rfq, 'shipper_address', None),
        'consignee_name': getattr(rfq, 'consignee_name', None),
        'consignee_address': getattr(rfq, 'consignee_address', None),
        'carrier_name': _chosen_carrier(quote),
        'cha_mode': _cha_mode(rfq),
        'pol_code': getattr(rfq, 'pol_code', None),
        'pol_name': (getattr(rfq, 'pol_name', None)
                     or getattr(rfq, 'origin', None)),
        'pod_code': getattr(rfq, 'pod_code', None),
        'pod_name': (getattr(rfq, 'pod_name', None)
                     or getattr(rfq, 'destination', None)),
        'hs_code': getattr(rfq, 'hs_code', None),
        'package_count': getattr(rfq, 'package_count', None),
        'package_kind': getattr(rfq, 'package_kind', None),
        'gross_weight_kg': _number(
            getattr(rfq, 'gross_weight_kg', None)),
        'volume_cbm': _number(getattr(rfq, 'volume_cbm', None)),
        'container_count': getattr(rfq, 'container_count', None),
        # Written only when true; `prune` drops a False, and at the
        # far end any value at all would mean dangerous.
        'is_dangerous': getattr(rfq, 'is_dangerous', None) or None,
        'dg_class': (getattr(rfq, 'dg_class', None)
                     if getattr(rfq, 'is_dangerous', None) else None),
        'un_number': (getattr(rfq, 'un_number', None)
                      if getattr(rfq, 'is_dangerous', None) else None),
        # The TMS is adding this; until then it hard-codes 'FCM'.
        'gst_treatment': tms_intake.gst_treatment_for(
            vertical,
            override=(gst_treatment
                      or getattr(quote, 'gst_treatment', None)),
            account=account),
    }
    # `is_dangerous` is never written: it is truthiness at the far
    # end, so any value at all means dangerous and the only way to
    # say no is to leave the field out.
    return tms_intake.prune(fields)


def payload_problems(fields):
    """What would stop this pack importing, said here instead.

    The TMS refuses in a fixed order and leaves nothing behind, so
    none of this is dangerous — but finding out from a rejection
    after somebody has filled in a workbook is a wasted afternoon.
    """
    problems = []
    for name in tms_intake.missing_required(fields):
        problems.append(_why_missing(name, fields))
    for name in tms_intake.unknown_fields(fields):
        problems.append(f'{name}: not a field the reader knows; it '
                        f'would be ignored without a word')
    return problems


def _why_missing(name, fields):
    if name == 'direction':
        return ('direction: the CRM does not record import or '
                'export, and the route was not recognisable enough '
                'to tell — fill it in as EXPORT, IMPORT or '
                'CROSS_TRADE')
    if name == 'mode':
        return ('mode: the TMS takes SEA or AIR only. A Multimodal '
                'lead has to be sent as whichever leg the job is '
                'filed under')
    if name == 'customer':
        return ('customer: must match a name on the TMS client '
                'register exactly; a pack never creates one')
    return f'{name}: required, and the CRM has no value for it'


def _chosen_carrier(quote):
    """Whoever won the freight leg, if one has been chosen.

    The TMS stores the carrier's *name* and leaves its vendor link
    null for somebody to join by hand, so a name is all it wants —
    and by handover time the choice has been made and recorded.
    """
    from app.models.ff import RateRequest
    from app.models.quote import QuoteLine

    line = (QuoteLine.query
            .filter(QuoteLine.quote_id == quote.id,
                    QuoteLine.charge_group == 'freight',
                    QuoteLine.source_rate_request_id.isnot(None))
            .order_by(QuoteLine.line_no.asc()).first())
    if line is None:
        return None
    req = db.session.get(RateRequest, line.source_rate_request_id)
    return req.vendor_name if req is not None else None


def _cha_mode(rfq):
    """OWN at Chennai and Mumbai — at whichever end clears customs.

    Which end that is depends on the direction, and getting it
    backwards is not cosmetic: it decides whether we are selling
    our own licence or buying somebody else's. An export clears at
    the port of loading, an import at the port of discharge, and a
    cross trade touches no Indian customs at all, so there is no
    own-CHA work on it whatever the ports say.

    The TMS spells the second value OUTSOURCED where this CRM says
    partner, and it does not validate the field, so a wrong word
    here would be stored as typed.
    """
    from app.models.ff import CHA_OWN
    from app.services import ff_rates

    direction = (getattr(rfq, 'direction', None) or '').upper()
    if direction == 'EXPORT':
        port = (getattr(rfq, 'pol_name', None)
                or getattr(rfq, 'origin', None))
    elif direction == 'IMPORT':
        port = (getattr(rfq, 'pod_name', None)
                or getattr(rfq, 'destination', None))
    elif direction == 'CROSS_TRADE':
        return 'OUTSOURCED'
    else:
        return None
    if not port:
        return None
    return 'OWN' if ff_rates.cha_mode_for(port) == CHA_OWN else 'OUTSOURCED'


def _resolve(quote, lead, account, rfq):
    """Fill in what the caller did not pass.

    `customer` is required and selects the client at the far end, so
    a pack built by a caller that happened not to hand us the
    account would be refused for a name the CRM knew all along.
    """
    from app import Company, Lead
    from app.models.rfq import RFQ

    if lead is None and getattr(quote, 'lead_id', None):
        lead = db.session.get(Lead, quote.lead_id)
    if account is None:
        if getattr(quote, 'account_id', None):
            account = db.session.get(Company, quote.account_id)
        elif lead is not None and lead.company_id:
            account = db.session.get(Company, lead.company_id)
    if rfq is None and getattr(quote, 'rfq_id', None):
        rfq = db.session.get(RFQ, quote.rfq_id)
    return lead, account, rfq


def _sell_value(quote):
    from app.services import ff_rates

    total = _number(getattr(quote, 'total_amount', None))
    if total:
        return total
    summary = ff_rates.margin_summary(quote)
    return summary['sell_inr'] or None


def _number(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
    problems = payload_problems(payload)
    to_fill = [(f, _fill_note(f)) for f in tms_intake.CANNOT_SUPPLY]
    for p in problems:
        field = p.split(':', 1)[0]
        to_fill.insert(0, (field, p))
    path = _write_xlsx(payload, quote, out_dir=out_dir,
                       to_fill=to_fill,
                       extra_sheets=(('Charges (not imported)',
                                      _charge_rows(quote)),))
    pack.payload_json = json.dumps(payload, indent=2, default=str)
    pack.file_path = path
    pack.built_at = datetime.utcnow()
    pack.built_by = actor
    pack.status = 'Ready'
    return pack, payload, path


#: Why the CRM has nothing to put in each one. Short, because it is
#: read in a spreadsheet cell by somebody filling a form in.
_FILL_NOTES = {
    'shipper_name': 'the CRM records an account and a contact, not '
                    'a shipper and a consignee',
    'consignee_name': 'as above — and on an import the consignee is '
                      'usually our own client',
    'carrier_name': 'known only once a rate is chosen; the chosen '
                    'vendor is on the Charges sheet',
    'overseas_agent_name': 'as above',
    'pol_code': 'UN/LOCODE for sea, IATA for air. The CRM holds the '
                'port as free text only, on the Shipment sheet',
    'pod_code': 'as above',
    'etd': 'no sailing or flight is booked at quotation',
    'eta': 'as above',
    'incoterm_place': 'the CRM records the Incoterm, not the named '
                      'place that completes it',
    'customer_code': 'the TMS client code is not held in the CRM',
}


def _fill_note(field):
    if field in _FILL_NOTES:
        return _FILL_NOTES[field]
    if field in ('hs_code', 'package_count', 'package_kind',
                 'gross_weight_kg', 'volume_cbm', 'container_count',
                 'dg_class', 'un_number'):
        return ('cargo detail: quoted from the enquiry, never '
                'stored as figures — take it from the booking')
    return 'the CRM has no column for this'


def _charge_rows(quote):
    """The priced lines, for a person to read. The TMS reader
    ignores this sheet: none of its column A values is a field name,
    which is deliberate — a sheet that answered a field twice would
    override the Shipment sheet."""
    from app.models.ff import RateRequest
    from app.models.quote import QuoteLine

    rows = [('Line', 'Service', 'Charge group', 'Description',
             'Sell (INR)', 'Cost (INR)', 'Vendor')]
    for ln in (QuoteLine.query.filter_by(quote_id=quote.id)
               .order_by(QuoteLine.line_no.asc()).all()):
        vendor = ''
        if ln.source_rate_request_id:
            rr = db.session.get(RateRequest, ln.source_rate_request_id)
            vendor = rr.vendor_name if rr else ''
        rows.append((ln.line_no, ln.service or '',
                     ln.charge_group or '', ln.description or '',
                     float(ln.line_total or 0),
                     float(ln.cost_inr) if ln.cost_inr is not None
                     else '', vendor))
    return rows


def _write_xlsx(payload, quote, *, out_dir=None, title=None,
                to_fill=(), extra_sheets=()):
    """The pack, as a workbook the TMS reader can take as it stands.

    Column A is the field name and column B the value, which is the
    whole of the reader's contract. Two things about it decide the
    layout here:

    - It walks **every sheet** and keeps any row whose column A is a
      name it knows, and where a name appears twice the **last
      sheet wins**. So the fields are written on exactly one sheet,
      and no other sheet may put a field name in column A. The
      "To fill in" sheet therefore lists its names in column *B*
      behind a label — a sheet listing them in A with an empty B
      would blank every value, because a blank cell is absent.
    - A blank cell is absent, not empty, so a field we have no
      answer for is simply not written.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    root = out_dir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), PACK_DIR)
    os.makedirs(root, exist_ok=True)
    name = (payload.get('reference') or payload.get('quote_number')
            or f'quote-{quote.id}')
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', str(name)).strip('_')
    path = os.path.join(root, f'ff_handover_{safe}.xlsx')

    wb = Workbook()
    head = Font(bold=True, color='FFFFFF')
    fill = PatternFill('solid', fgColor='1F2937')
    wrap = Alignment(wrap_text=True, vertical='top')

    ws = wb.active
    ws.title = 'Shipment'
    for field in tms_intake.PART_A_FIELDS + ('gst_treatment',):
        if field in payload:
            ws.append([field, payload[field]])
    ws.column_dimensions['A'].width = 22
    ws.column_dimensions['B'].width = 52

    if to_fill:
        fs = wb.create_sheet('To fill in')
        fs.append(['Before importing, add these on the Shipment '
                   'sheet. They are in column B here on purpose: a '
                   'field name in column A of any sheet is read, '
                   'and the last sheet wins, so listing them there '
                   'would wipe what is already filled.'])
        fs.cell(1, 1).alignment = wrap
        fs.column_dimensions['A'].width = 18
        fs.column_dimensions['B'].width = 30
        fs.column_dimensions['C'].width = 60
        fs.append([])
        fs.append(['Fill in:', 'field', 'note'])
        for cell in fs[3]:
            cell.font, cell.fill = head, fill
        for field, note in to_fill:
            fs.append(['', field, note])
            fs.cell(fs.max_row, 3).alignment = wrap

    for sheet_name, rows in extra_sheets:
        es = wb.create_sheet(sheet_name[:28])
        for i, row in enumerate(rows):
            es.append(list(row))
            if i == 0:
                for cell in es[1]:
                    cell.font, cell.fill = head, fill
        for col, width in (('A', 8), ('B', 26), ('C', 20),
                           ('D', 40), ('E', 14), ('F', 14)):
            es.column_dimensions[col].width = width

    wb.save(path)
    return path
