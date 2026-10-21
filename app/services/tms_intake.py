"""
The TMS intake formats, as the TMS actually reads them.

Source: `docs/tms_import_formats.md`, copied from Procam LR
(Amiish14/procam-lr, commit c8e6e85). Field names here are that
document's, not ours. Before this, both handover builders emitted a
CRM-shaped payload and carried an `INVENTED` list saying so.

Two formats, and they are not alike

  Part A  a freight job arrives as one Handover Pack: a flat set of
          fields, read from JSON or from a workbook where column A
          is the field name and column B the value. The TMS creates
          the project, job, FF detail and client order from it.

  Part B  every other job is typed by a person — project, then job,
          then client order. The project code and the job number are
          typed, never generated, so the CRM must not invent them.
          We send what a person would otherwise retype.

What this module will not do

  It does not fill a field the CRM has no answer for. The CRM holds
  no shipper, no consignee, no HS code, no weights and no
  import/export direction, and a pack with a guessed `direction`
  creates a job pointing the wrong way. Absent is a cell left blank,
  which the reader treats as "not in the pack", and for a required
  field the import refuses — loudly, having created nothing. That is
  the correct outcome and is better than a plausible wrong value.

  `CANNOT_SUPPLY` names those fields so the pack can print them as a
  short list of what a person must add before importing.
"""
from __future__ import annotations

# ── Part A: the FF Handover Pack ─────────────────────────────────────
#: Every field the reader knows, in the document's order. The pack
#: must use exactly these spellings; anything else is ignored by the
#: reader without complaint, which is the quiet way to lose a value.
PART_A_FIELDS = (
    # Shipment
    'reference', 'customer', 'customer_code', 'mode', 'direction',
    'incoterm', 'incoterm_place', 'quote_reference',
    'tally_po_number', 'sell_value', 'currency',
    # Parties
    'shipper_name', 'shipper_address', 'consignee_name',
    'consignee_address', 'notify_name', 'notify_address',
    'carrier_name', 'overseas_agent_name', 'cha_mode',
    # Route
    'pol_code', 'pol_name', 'pod_code', 'pod_name', 'etd', 'eta',
    # Cargo
    'hs_code', 'package_count', 'package_kind', 'gross_weight_kg',
    'volume_cbm', 'container_count', 'is_dangerous', 'dg_class',
    'un_number',
)

#: The import refuses without these four.
PART_A_REQUIRED = ('reference', 'customer', 'mode', 'direction')

MODES = ('SEA', 'AIR')
DIRECTIONS = ('EXPORT', 'IMPORT', 'CROSS_TRADE')
CHA_MODES = ('OWN', 'OUTSOURCED')

#: Read by the TMS and stored on no business record. Sent anyway
#: where we have them — they survive in `ff_handover_packs.payload`,
#: which is where somebody looks when a figure is questioned — but
#: nothing downstream should expect them back.
READ_AND_DROPPED = ('customer_code', 'quote_reference', 'scope',
                    'currency')

#: Fields the format defines that the CRM still has no answer for.
#: Not "invented": the opposite. These are the gaps a person fills
#: on the pack before importing.
#:
#: It was nineteen. The enquiry form now captures the shipment —
#: parties, ports, cargo, dangerous goods — so what is left is the
#: four things that genuinely are not known at quotation and the
#: two the CRM has no concept of.
CANNOT_SUPPLY = (
    # Not known until a booking exists, which is after the win.
    'etd', 'eta',
    # A notify party is a bill-of-lading instruction, not a sales
    # fact; nobody is asked for one while quoting.
    'notify_name', 'notify_address',
    # The destination agent is chosen when the job is operated.
    'overseas_agent_name',
    # The TMS's own code for the client. The CRM matches by name,
    # which is what the reader does too.
    'customer_code',
)

#: `is_dangerous` is `bool(value)` at the far end, so "No", "n/a"
#: and a text "0" all mean dangerous. The only way to say no is to
#: leave it out, so this module never writes it unless it is true.
DANGEROUS_ONLY_WHEN_TRUE = 'is_dangerous'


# ── Part B: the typed job ────────────────────────────────────────────
#: Issued by a person in the TMS, never by us. The CRM sending a
#: `PRO…`/`JOB…` number would be inventing an identifier in somebody
#: else's series.
THEIRS_TO_NUMBER = ('code', 'pcn_job_no', 'po_number')

PART_B_PROJECT = (
    'name', 'client_name', 'vertical', 'status', 'currency',
    'start_date', 'end_date', 'project_value', 'short_description',
    'linked_lead_number',
)
PART_B_JOB = (
    'name', 'currency', 'job_value', 'job_type', 'mod',
    'cargo_description', 'cargo_weight_kg', 'cargo_packages',
    'shipper_name', 'consignee_name', 'supplier_cha_name',
    'discharge_port', 'final_site', 'sbu_unit',
    'client_billing_method',
)
PART_B_ORDER = (
    'client_name', 'client_po_number', 'gst_treatment', 'po_date',
    'po_value', 'currency',
)

#: The six the TMS accepts. Legacy 'FCM' / 'RCM' are aliased at the
#: far end to the Transport variants; we send the full names.
GST_TREATMENTS = ('TRANSPORT_RCM', 'TRANSPORT_FCM',
                  'TRANSPORT_EXEMPT', 'GOODS_5', 'GOODS_18',
                  'GOODS_28')

#: By vertical, from the worked examples in Part B: PTM-H and BLADE
#: are TRANSPORT_FCM, PFM, INSTALLATION and WAREHOUSE are GOODS_18.
#:
#: TRANSPORT_RCM is deliberately not derivable. It means the client
#: pays the tax direct, which is an arrangement with that client and
#: is recorded nowhere in this CRM. Guessing it would under-bill;
#: defaulting to FCM over-collects and is corrected on one screen.
#: So the default is FCM and the caller may override.
BY_VERTICAL = {
    'Freight Forwarding': 'GOODS_18',
    'Project Freight': 'GOODS_18',
    'General Transport': 'TRANSPORT_FCM',
    'Heavy Transport': 'TRANSPORT_FCM',
    'Installation': 'GOODS_18',
    'Warehousing': 'GOODS_18',
}
DEFAULT_GST = 'TRANSPORT_FCM'


class IntakeError(ValueError):
    """A value the TMS would refuse, caught before it is sent."""


def check_treatment(value):
    text = str(value or '').strip().upper()
    if text not in GST_TREATMENTS:
        raise IntakeError(
            f'{value!r} is not one of the six the TMS accepts: '
            f'{", ".join(GST_TREATMENTS)}.')
    return text


def gst_treatment_for(vertical, *, override=None, account=None):
    """Which of the six this job is billed under.

    In order: what somebody set on this quote, then what is set on
    the customer, then the vertical. The customer sits in the
    middle because that is where RCM lives — it means they pay the
    tax direct, which is an arrangement with them and is true of
    every job we do for them until it is not.

    TRANSPORT_RCM is still never reached by the vertical alone. A
    vertical cannot know it, and guessing it under-bills.
    """
    if override:
        return check_treatment(override)
    on_account = (getattr(account, 'default_gst_treatment', None)
                  if account is not None else None)
    if on_account:
        return check_treatment(on_account)
    return BY_VERTICAL.get((vertical or '').strip(), DEFAULT_GST)


def mode_for(lead_mode):
    """`Sea` -> `SEA`. Multimodal has no TMS value, so None.

    Returning None rather than picking one leaves `mode` out of the
    pack, and the import refuses on a required field instead of
    creating a sea job for an air-and-road movement.
    """
    value = (lead_mode or '').strip().upper()
    return value if value in MODES else None


#: Enough to recognise an Indian port or city in free text. The CRM
#: records a route as two free-text boxes, so this is the only way
#: to reach a direction at all.
_INDIA = (
    'india', 'inmaa', 'innsa', 'inbom', 'inmun', 'inixy', 'incok',
    'inpav', 'inkat', 'intut', 'inhzr', 'invtz', 'inccu', 'inblr',
    'chennai', 'mumbai', 'nhava sheva', 'jnpt', 'mundra', 'kandla',
    'cochin', 'kochi', 'tuticorin', 'visakhapatnam', 'vizag',
    'kolkata', 'calcutta', 'ennore', 'hazira', 'pipavav', 'krishnapatnam',
    'bangalore', 'bengaluru', 'delhi', 'hyderabad', 'ahmedabad',
    'pune', 'goa', 'paradip', 'haldia', 'tuticorin', 'mangalore',
)


def _is_india(place):
    low = (place or '').strip().lower()
    return bool(low) and any(k in low for k in _INDIA)


def direction_for(origin, destination):
    """EXPORT / IMPORT / CROSS_TRADE, or None when it cannot be told.

    `direction` is required and the CRM does not record it, so it is
    read off the route. Both ends have to be recognisable: one
    Indian end and one blank is not a direction, it is half a route,
    and a pack guessing EXPORT there creates a job pointing the
    wrong way through customs.
    """
    if not (origin or '').strip() or not (destination or '').strip():
        return None
    from_in, to_in = _is_india(origin), _is_india(destination)
    if from_in and not to_in:
        return 'EXPORT'
    if to_in and not from_in:
        return 'IMPORT'
    if not from_in and not to_in:
        return 'CROSS_TRADE'
    # Both ends read as Indian: a domestic move is not a freight
    # forwarding direction at all.
    return None


def prune(fields):
    """Drop what we have no answer for. Blank is absent, not empty.

    A key carrying '' or None would be written into the workbook as
    an empty cell, which the reader treats as absent anyway — but in
    JSON it is a key with a null, and the far end does not test for
    that everywhere. Dropping it here means one meaning of missing.
    """
    out = {}
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if key == DANGEROUS_ONLY_WHEN_TRUE and not value:
            continue
        out[key] = value
    return out


def missing_required(fields):
    return [f for f in PART_A_REQUIRED if not fields.get(f)]


def unknown_fields(fields):
    """Names the reader would silently ignore. A typo loses a value
    without a word, so the pack checks its own spelling."""
    known = set(PART_A_FIELDS) | {'gst_treatment'}
    return sorted(k for k in fields if k not in known)
