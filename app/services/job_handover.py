"""
The Won pack for the verticals that are not forwarding.

CRM 2.0 R5. A forwarding win arrives as a Handover Pack and is
imported. Every other job is **typed** into the TMS — project, then
job, then client order — so this pack is what a person retypes
from, not a file a reader consumes.

Source: `docs/tms_import_formats.md`, Part B, from Procam LR at
commit c8e6e85. Until that arrived this module carried an
`INVENTED` list of field names it had made up; the names below are
theirs.

What we do not send, on purpose
    The project code and the job number are typed by a person in
    the TMS — nothing there generates them, and the job number
    carries its project code so the two halves must agree. A number
    minted here would be an identifier in somebody else's series,
    and `job_lookup.project_named_in()` would reject a job whose
    code named a project that did not exist. The client order
    number is the opposite case: the TMS assigns it from a
    per-company series, so sending one would be overwritten at
    best.

    `tms_intake.THEIRS_TO_NUMBER` names all three.

    What we do send is the customer's own number, as
    `client_po_number`. That is a reference the TMS keeps beside its
    own, and it is the one number on the order that is genuinely
    ours to supply.
"""
from __future__ import annotations

import os
from datetime import datetime

from app import db
from app.services import tms_intake

#: Fields the format defines that the CRM has no column for. The
#: replacement for the old INVENTED list, and the opposite of it:
#: these names are real and the gaps are ours.
CANNOT_SUPPLY = (
    'branch_id', 'project_head_id', 'fiscal_year', 'phase',
    'full_job_code', 'splendin_job_no', 'job_owner_id',
    'cargo_dimension_unit', 'frt_rate_per_unit', 'frt_unit',
    'frt_volume_unit', 'procam_gst_reg_id', 'business_id',
    'client_id',
)

#: Issued at the far end, never here.
THEIRS_TO_NUMBER = tms_intake.THEIRS_TO_NUMBER

PACK_DIR = os.path.join('docs', 'exports', 'job_handover')

#: `vertical` is a free-text box in the TMS and production holds
#: twelve spellings for six verticals. We send the spelling their
#: own worked examples use, which is the closest thing to a
#: vocabulary that exists.
VERTICAL_CODE = {
    'Heavy Transport': 'PTM-H',
    'General Transport': 'PTM-M',
    'Project Freight': 'PFM',
    'Freight Forwarding': 'PFM',
    'Installation': 'INSTALLATION',
    'Warehousing': 'WAREHOUSE',
}

#: `job_type` and `mod` from the same examples.
JOB_TYPE = {
    'Heavy Transport': 'Road',
    'General Transport': 'Road',
    'Project Freight': 'Sea',
    'Freight Forwarding': 'Sea',
    'Installation': 'Installation',
    'Warehousing': 'Warehouse',
}
MOD = dict(JOB_TYPE)


def vertical_code_for(vertical):
    return VERTICAL_CODE.get((vertical or '').strip(), 'PTM-M')


def job_type_for(vertical):
    return JOB_TYPE.get((vertical or '').strip(), 'Road')


def build_payload(quote, *, pack=None, lead=None, account=None,
                  rfq=None, gst_treatment=None):
    """Project, job and client order, in the TMS's Part B names.

    Three blocks because they are three screens, filled in that
    order, and a value is only useful next to the field it goes in.
    """
    from app.models.ff import FfHandoverPack
    from app.models.quote import QuoteLine
    from app.services import ff_handover, ff_rates, quote_terms

    pack = pack or FfHandoverPack.query.filter_by(
        quote_id=quote.id).first()
    lead, account, rfq = ff_handover._resolve(quote, lead, account,
                                              rfq)
    lines = (QuoteLine.query.filter_by(quote_id=quote.id)
             .order_by(QuoteLine.line_no.asc()).all())
    vertical = quote_terms.vertical_of(quote)
    margin = ff_rates.margin_summary(quote)
    terms = quote_terms.for_print(quote)
    currency = getattr(quote, 'currency', None) or 'INR'
    customer = (getattr(account, 'name', None)
                or getattr(lead, 'company', None) or '')
    total = ff_handover._sell_value(quote)

    project = tms_intake.prune({
        'name': _project_name(quote, lead, rfq, vertical),
        'client_name': customer,
        'vertical': vertical_code_for(vertical),
        'status': 'Active',
        'currency': currency,
        'project_value': total,
        'short_description': getattr(rfq, 'scope', None),
        # Their own optional field, and the one that makes the two
        # systems joinable without a new table.
        'linked_lead_number': (str(lead.id)
                               if lead is not None else None),
    })

    job = tms_intake.prune({
        'name': _job_name(rfq, lead, vertical),
        'currency': currency,
        'job_value': total,
        'job_type': job_type_for(vertical),
        'mod': MOD.get(vertical, 'Road'),
        'cargo_description': getattr(rfq, 'cargo', None),
        'discharge_port': (getattr(rfq, 'destination', None)
                           if vertical in ('Project Freight',
                                           'Freight Forwarding')
                           else None),
        'final_site': (getattr(rfq, 'destination', None)
                       if vertical in ('Installation',
                                       'Heavy Transport',
                                       'General Transport')
                       else None),
        # PER_VEHICLE or PER_FRT decides how an LR is priced, and
        # the CRM does not record which: a quote line is a price,
        # not a billing method. Left for the person typing the job.
        'client_billing_method': None,
    })

    order = tms_intake.prune({
        'client_name': customer,
        'client_po_number': getattr(pack, 'client_po_number', None),
        'gst_treatment': tms_intake.gst_treatment_for(
            vertical, override=gst_treatment),
        'po_date': str(datetime.utcnow().date()),
        'po_value': total,
        'currency': currency,
    })

    return {
        'crm_quote_id': quote.id,
        'crm_lead_id': getattr(quote, 'lead_id', None),
        'crm_account_id': getattr(quote, 'account_id', None),
        'crn': getattr(quote, 'crn', '') or '',
        'quote_number': getattr(quote, 'quote_number', '') or '',
        'project': project,
        'job': job,
        'client_order': order,
        'charges': _charges(lines),
        'equipment': _equipment_from(lines),
        'terms': {
            'standard_title': terms['standard']['title'],
            'version': terms['standard']['version'],
            'special': [c['text'] for c in terms['special']],
        },
        'totals': {
            'sell_inr': margin['sell_inr'],
            'cost_inr': margin['cost_inr'],
            'margin_inr': margin['margin_inr'],
            'currency': currency,
            'margin_reliable': margin['reliable'],
        },
        'confirmation': {
            'received_at': (str(pack.confirmed_at)[:19]
                            if pack and pack.confirmed_at else ''),
            'from': (pack.confirmed_by_email if pack else ''),
            'subject': (pack.confirmation_subject if pack else ''),
            'message_id': (pack.confirmation_message_id
                           if pack else ''),
        },
        'built_at': datetime.utcnow().isoformat(
            timespec='seconds') + 'Z',
        'format_note': (
            'Part B of docs/tms_import_formats.md. The project '
            'code, the job number and the order number are typed '
            'or assigned in the TMS and are deliberately absent '
            'here.'),
    }


def _project_name(quote, lead, rfq, vertical):
    for candidate in (getattr(lead, 'project', None),
                      getattr(rfq, 'subject', None),
                      getattr(quote, 'subject', None)):
        if (candidate or '').strip():
            return candidate.strip()[:200]
    return f'{vertical} — {getattr(lead, "company", "") or ""}'.strip(
        ' —')[:200]


def _job_name(rfq, lead, vertical):
    """Their examples name a job for the movement, in capitals."""
    origin = (getattr(rfq, 'origin', None) or '').strip()
    dest = (getattr(rfq, 'destination', None) or '').strip()
    cargo = (getattr(rfq, 'cargo', None) or '').strip()
    bits = [b for b in (cargo.upper() if cargo else '',
                        f'{origin.upper()} TO {dest.upper()}'
                        if origin and dest else '') if b]
    if bits:
        return ' // '.join(bits)[:500]
    return (getattr(lead, 'project', None)
            or getattr(lead, 'company', None) or vertical)[:500]


def _charges(lines):
    from app.models.ff import RateRequest

    out = []
    for ln in lines:
        vendor = None
        if ln.source_rate_request_id:
            rr = db.session.get(RateRequest, ln.source_rate_request_id)
            vendor = rr.vendor_name if rr else None
        out.append({
            'line_no': ln.line_no,
            'service': ln.service or '',
            'description': ln.description or '',
            'charge_group': ln.charge_group or 'other',
            'quantity': (float(ln.quantity)
                         if ln.quantity is not None else None),
            'unit': ln.unit or '',
            'sell_inr': float(ln.line_total or 0),
            'cost_inr': (float(ln.cost_inr)
                         if ln.cost_inr is not None else None),
            'vendor': vendor,
        })
    return out


def _number(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _equipment_from(lines):
    """What the quote says is being brought to site.

    Read off the equipment-hire lines rather than asked for
    separately: a second field would disagree with the priced lines
    within a week.
    """
    return [
        {'description': (ln.description or ln.service or '').strip(),
         'quantity': (float(ln.quantity)
                      if ln.quantity is not None else None),
         'unit': ln.unit or ''}
        for ln in lines
        if (ln.charge_group or '') == 'equipment'
    ]


def build_pack(quote, *, actor=None, lead=None, account=None,
               rfq=None, out_dir=None):
    """Write the worksheet and store the payload beside it.

    Its own writer, not ff_handover's. That one produces the
    machine-readable Part A shape — one field name per row, because
    a reader consumes it. Nothing reads this: a person types it into
    three screens, so it is laid out as those three screens. Sharing
    a writer between them would mean one of the two is wrong for its
    reader.
    """
    import json

    from app.models.ff import FfHandoverPack

    payload = build_payload(quote, lead=lead, account=account,
                            rfq=rfq)
    pack = FfHandoverPack.query.filter_by(quote_id=quote.id).first()
    if pack is None:
        pack = FfHandoverPack(quote_id=quote.id)
        db.session.add(pack)
        db.session.flush()
    path = _write_xlsx(payload, quote, out_dir=out_dir)
    pack.payload_json = json.dumps(payload, indent=2, default=str)
    pack.file_path = path
    pack.built_at = datetime.utcnow()
    pack.built_by = actor
    return pack, payload


def _write_xlsx(payload, quote, *, out_dir=None):
    """Three blocks, in the order they are typed."""
    import re

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    root = out_dir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), PACK_DIR)
    os.makedirs(root, exist_ok=True)
    name = (payload.get('crn') or payload.get('quote_number')
            or f'quote-{quote.id}')
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', str(name)).strip('_')
    path = os.path.join(root, f'job_handover_{safe}.xlsx')

    wb = Workbook()
    head = Font(bold=True, color='FFFFFF')
    fill = PatternFill('solid', fgColor='1F2937')
    wrap = Alignment(wrap_text=True, vertical='top')

    ws = wb.active
    ws.title = 'Type into the TMS'
    ws.append([f'{payload["project"].get("vertical", "")} job — '
               f'{payload["project"].get("client_name", "")}'])
    ws['A1'].font = Font(bold=True, size=13)
    ws.append([payload['format_note']])
    ws.cell(2, 1).alignment = wrap
    ws.append([])

    for title, block, note in (
            ('1. Project  (POST /projects/new)', payload['project'],
             'code is typed by you and must be unique; client and '
             'company are chosen from the masters'),
            ('2. Job  (POST /projects/<id>/jobs/new)', payload['job'],
             'pcn_job_no is typed by you and must name the project '
             'above; client_billing_method decides how the LR is '
             'priced'),
            ('3. Client order  (POST /commercials/pos/new)',
             payload['client_order'],
             'po_number is assigned by the TMS; gst_treatment is '
             'one of the six')):
        ws.append([title])
        ws.cell(ws.max_row, 1).font = Font(bold=True)
        ws.append(['', note])
        ws.cell(ws.max_row, 2).alignment = wrap
        ws.append(['field', 'value'])
        for cell in ws[ws.max_row]:
            cell.font, cell.fill = head, fill
        for key, value in block.items():
            ws.append([key, value])
        for key in THEIRS_TO_NUMBER:
            ws.append([key, '— you type this —'])
        ws.append([])
    ws.column_dimensions['A'].width = 26
    ws.column_dimensions['B'].width = 62

    cs = wb.create_sheet('Charges')
    cs.append(['Line', 'Service', 'Charge group', 'Description',
               'Qty', 'Unit', 'Sell (INR)', 'Cost (INR)', 'Vendor'])
    for cell in cs[1]:
        cell.font, cell.fill = head, fill
    for c in payload['charges']:
        cs.append([c['line_no'], c['service'], c['charge_group'],
                   c['description'], c['quantity'], c['unit'],
                   c['sell_inr'], c['cost_inr'], c['vendor'] or ''])
    t = payload['totals']
    cs.append([])
    cs.append(['', 'TOTAL', '', '', '', '', t['sell_inr'],
               t['cost_inr'], ''])
    cs.cell(cs.max_row, 2).font = Font(bold=True)
    if not t['margin_reliable']:
        cs.append(['', 'Some lines carry no cost, so the margin '
                       'above is not the margin.'])
    for col, w in (('A', 7), ('B', 24), ('C', 18), ('D', 40),
                   ('E', 8), ('F', 10), ('G', 14), ('H', 14),
                   ('I', 24)):
        cs.column_dimensions[col].width = w

    gaps = wb.create_sheet('Not from the CRM')
    gaps.append(['These the TMS asks for and the CRM does not hold.'])
    gaps.append([])
    gaps.append(['field'])
    gaps.cell(3, 1).font, gaps.cell(3, 1).fill = head, fill
    for field in CANNOT_SUPPLY:
        gaps.append([field])
    gaps.column_dimensions['A'].width = 28

    wb.save(path)
    return path
