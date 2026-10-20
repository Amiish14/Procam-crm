"""
The Won pack for the verticals that are not forwarding.

CRM 2.0 R5. A forwarding win becomes a shipment; a transport,
installation or warehousing win becomes a **Job** in the TMS. This
builds that payload from the same quote, the same lines and the same
confirmation capture `ff_handover` already uses — it does not keep a
second copy of any of them.

The shape is a guess, and says so
    FF-COMMS-02 never arrived and no TMS Job import contract exists
    in this repository either: `tms_job_id` appears on an integration
    link and nothing describes what creates one. So the field names
    below are derived from what the CRM actually knows, and every
    field that is a guess is listed in `INVENTED`.

    The payload is **stored, not sent**. Nothing here calls the
    production TMS, and that stays true until somebody puts the real
    contract in front of it.

Why not one builder with an `if`
    `ff_handover.build_payload` answers "what shipment is this": mode,
    Incoterm, which party is our client, chargeable weight. None of
    that exists on a crane job. A single builder carrying both would
    emit a shipment block full of empty strings and a reader would
    not be able to tell "not applicable" from "nobody filled it in".
"""
from __future__ import annotations

import os
from datetime import datetime

from app import db

#: Fields invented here because nothing on this system defines them.
#: Read this list before trusting the payload downstream.
INVENTED = (
    'event', 'job.job_type', 'job.site_from', 'job.site_to',
    'job.scope', 'job.equipment', 'job.start_by',
    'charges[].charge_group', 'charges[].cost_inr',
    'charges[].vendor', 'totals.margin_inr', 'terms.version',
)

PACK_DIR = os.path.join('docs', 'exports', 'job_handover')

#: What the TMS would call this kind of work. A guess, per the note
#: above, but a guess that at least distinguishes the verticals
#: rather than sending them all as one type.
JOB_TYPE = {
    'General Transport': 'ROAD',
    'Heavy Transport': 'ODC',
    'Project Freight': 'PROJECT',
    'Installation': 'INSTALL',
    'Warehousing': 'WAREHOUSE',
}


def job_type_for(vertical):
    return JOB_TYPE.get((vertical or '').strip(), 'GENERAL')


def build_payload(quote, *, pack=None, lead=None, account=None,
                  rfq=None):
    """The TMS Job payload, built and returned. Never sent."""
    from app.models.ff import FfHandoverPack, RateRequest
    from app.models.quote import QuoteLine
    from app.services import ff_rates, quote_terms

    pack = pack or FfHandoverPack.query.filter_by(
        quote_id=quote.id).first()
    lines = (QuoteLine.query.filter_by(quote_id=quote.id)
             .order_by(QuoteLine.line_no.asc()).all())
    vertical = quote_terms.vertical_of(quote)

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
            'quantity': (float(ln.quantity)
                         if ln.quantity is not None else None),
            'unit': ln.unit or '',
            'sell_inr': float(ln.line_total or 0),
            'cost_inr': (float(ln.cost_inr)
                         if ln.cost_inr is not None else None),
            'vendor': vendor,
        })

    margin = ff_rates.margin_summary(quote)
    terms = quote_terms.for_print(quote)
    return {
        'event': 'crm.job.handover',
        'crm_quote_id': quote.id,
        'crm_lead_id': getattr(quote, 'lead_id', None),
        'crm_account_id': getattr(quote, 'account_id', None),
        'crn': getattr(quote, 'crn', '') or '',
        'quote_number': getattr(quote, 'quote_number', '') or '',
        'job': {
            'vertical': vertical,
            'job_type': job_type_for(vertical),
            'site_from': getattr(rfq, 'origin', None) or '',
            'site_to': getattr(rfq, 'destination', None) or '',
            'scope': getattr(rfq, 'scope', None) or '',
            'cargo': getattr(rfq, 'cargo', None) or '',
            'equipment': _equipment_from(lines),
            'start_by': (str(rfq.quote_by_date)
                         if rfq is not None
                         and getattr(rfq, 'quote_by_date', None)
                         else ''),
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
        'terms': {
            'standard_title': terms['standard']['title'],
            'version': terms['standard']['version'],
            'special': [c['text'] for c in terms['special']],
        },
        'totals': {
            'sell_inr': margin['sell_inr'],
            'cost_inr': margin['cost_inr'],
            'margin_inr': margin['margin_inr'],
            'currency': getattr(quote, 'currency', None) or 'INR',
            'margin_reliable': margin['reliable'],
        },
        'built_at': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
        'format_note': ('Derived from CRM data. No TMS Job import '
                        'contract exists on this system; invented '
                        'fields are listed in '
                        'app/services/job_handover.INVENTED.'),
    }


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
    """Write the pack and store the payload beside it.

    Delegates the writing to ff_handover, which already knows how to
    turn a payload into a sheet operations can read, and how to
    record the pack row. Only the payload differs.
    """
    from app.models.ff import FfHandoverPack
    from app.services import ff_handover

    payload = build_payload(quote, lead=lead, account=account, rfq=rfq)
    pack = FfHandoverPack.query.filter_by(quote_id=quote.id).first()
    if pack is None:
        pack = FfHandoverPack(quote_id=quote.id)
        db.session.add(pack)
        db.session.flush()
    path = ff_handover._write_xlsx(
        payload, quote, out_dir=out_dir or PACK_DIR)
    pack.payload_json = payload
    pack.pack_path = path
    pack.built_at = datetime.utcnow()
    pack.built_by = actor
    return pack, payload
