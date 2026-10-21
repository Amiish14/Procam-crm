"""
The shipment behind a forwarding enquiry, captured once.

CRM 2.0 R5 / TMS Part A. Until now these facts lived nowhere: the
quote knew a price and a route as two free-text boxes, the rate
request described the leg in prose, and the handover pack left
nineteen fields blank for somebody at the TMS end to fill in from
memory. The same facts, typed twice, a fortnight apart.

Direction is asked, not inferred
    `tms_intake.direction_for` reads a direction off the two ends
    of the route, and it is right often enough to be worth
    offering. It is offered — `suggest()` pre-fills the field — and
    it is never stored on the strength of the guess alone. The
    difference matters because direction decides which customs
    regime a job goes through, and "Chennai to Mumbai" is not a
    forwarding movement at all while reading like one.

What is required, and why those five
    direction, POL, POD, gross weight, package count. They are the
    fields a carrier needs before it will price anything, so an
    enquiry without them cannot be sent for rates either — the
    requirement is the same requirement, enforced once.

    POL and POD take a code or a name. Requiring both would stop
    work on the first enquiry whose agent gave a port name and no
    LOCODE, which is most of them.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from app.services import tms_intake

DIRECTIONS = tms_intake.DIRECTIONS

#: (field, label) — everything the enquiry form captures.
FIELDS = (
    ('direction', 'Direction'),
    ('incoterm_place', 'Incoterm place'),
    ('shipper_name', 'Shipper'),
    ('shipper_address', 'Shipper address'),
    ('consignee_name', 'Consignee'),
    ('consignee_address', 'Consignee address'),
    ('pol_code', 'POL code'),
    ('pol_name', 'POL name'),
    ('pod_code', 'POD code'),
    ('pod_name', 'POD name'),
    ('hs_code', 'HS code'),
    ('package_count', 'Packages'),
    ('package_kind', 'Package kind'),
    ('gross_weight_kg', 'Gross weight (kg)'),
    ('volume_cbm', 'Volume (CBM)'),
    ('container_count', 'Containers'),
    ('is_dangerous', 'Dangerous goods'),
    ('dg_class', 'DG class'),
    ('un_number', 'UN number'),
)

#: Lives on the quote, not the RFQ: it completes the Incoterm.
ON_THE_QUOTE = ('incoterm_place',)

REQUIRED = ('direction', 'pol', 'pod', 'gross_weight_kg',
            'package_count')

INTEGERS = ('package_count', 'container_count')
DECIMALS = ('gross_weight_kg', 'volume_cbm')


class ShipmentError(ValueError):
    """What the form should say, in the words it should say it."""


def suggest(rfq):
    """What to pre-fill, from what the enquiry already says.

    A suggestion, returned separately from the stored value so a
    screen can show it as one. Nothing here writes.
    """
    return {
        'direction': tms_intake.direction_for(
            getattr(rfq, 'origin', None),
            getattr(rfq, 'destination', None)),
        'pol_name': (getattr(rfq, 'pol_name', None)
                     or getattr(rfq, 'origin', None)),
        'pod_name': (getattr(rfq, 'pod_name', None)
                     or getattr(rfq, 'destination', None)),
        'why': ('read off the route on the enquiry — check it, '
                'nothing is saved until you do'),
    }


def _clean(key, raw):
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return None
    if key in INTEGERS:
        try:
            value = int(str(raw).replace(',', ''))
        except (TypeError, ValueError):
            raise ShipmentError(f'{key}: {raw!r} is not a whole '
                                f'number.')
        if value < 0:
            raise ShipmentError(f'{key}: cannot be negative.')
        return value
    if key in DECIMALS:
        try:
            value = Decimal(str(raw).replace(',', ''))
        except (TypeError, InvalidOperation):
            raise ShipmentError(f'{key}: {raw!r} is not a number.')
        if value < 0:
            raise ShipmentError(f'{key}: cannot be negative.')
        return value
    if key == 'is_dangerous':
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ('1', 'true', 'yes', 'y', 'on'):
            return True
        if text in ('0', 'false', 'no', 'n', 'off'):
            return False
        raise ShipmentError('is_dangerous: answer yes or no.')
    return raw


def validate(data):
    """[(field, message)] — everything wrong, not just the first.

    All of it, because a form that reveals one problem per save is
    a form somebody saves six times.
    """
    problems = []
    direction = (data.get('direction') or '').strip().upper()
    if not direction:
        problems.append(('direction',
                         'Say whether this is an export, an import '
                         'or a cross trade. It decides which customs '
                         'regime the job goes through.'))
    elif direction not in DIRECTIONS:
        problems.append(('direction',
                         f'{direction} is not one of '
                         f'{", ".join(DIRECTIONS)}.'))

    if not (data.get('pol_code') or data.get('pol_name')):
        problems.append(('pol_name',
                         'Give the port of loading, as a code or a '
                         'name.'))
    if not (data.get('pod_code') or data.get('pod_name')):
        problems.append(('pod_name',
                         'Give the port of discharge, as a code or '
                         'a name.'))

    for key, label in (('gross_weight_kg', 'gross weight'),
                       ('package_count', 'package count')):
        raw = data.get(key)
        if raw in (None, ''):
            problems.append((key, f'A carrier will not price a '
                                  f'shipment without its {label}.'))
            continue
        try:
            value = _clean(key, raw)
        except ShipmentError as exc:
            problems.append((key, str(exc)))
            continue
        if not value:
            problems.append((key, f'The {label} cannot be nothing.'))

    if _clean('is_dangerous', data.get('is_dangerous')) is True:
        if not (data.get('dg_class') or '').strip():
            problems.append(('dg_class',
                             'Dangerous goods need their class.'))
        if not (data.get('un_number') or '').strip():
            problems.append(('un_number',
                             'Dangerous goods need their UN '
                             'number.'))
    return problems


def apply_to(rfq, data, *, quote=None, actor=None):
    """Write the shipment onto the enquiry. Caller commits."""
    from app.services import audit

    problems = validate(data)
    if problems:
        raise ShipmentError('; '.join(m for _f, m in problems))

    before, after = {}, {}
    for key, _label in FIELDS:
        if key not in data:
            continue
        target = quote if key in ON_THE_QUOTE else rfq
        if target is None:
            continue
        value = _clean(key, data.get(key))
        if key == 'direction' and value:
            value = str(value).strip().upper()
        was = getattr(target, key, None)
        if str(was) == str(value):
            continue
        setattr(target, key, value)
        before[key] = None if was is None else str(was)
        after[key] = None if value is None else str(value)

    if after:
        audit.record('rfq.shipment_updated', 'rfq', str(rfq.id),
                     old=before, new=after, actor=actor,
                     reason='the shipment detail the carrier, the '
                            'quote and the TMS pack all read')
    return after


def as_dict(rfq, quote=None):
    out = {}
    for key, _label in FIELDS:
        source = quote if key in ON_THE_QUOTE else rfq
        value = getattr(source, key, None) if source is not None else None
        if isinstance(value, Decimal):
            value = float(value)
        out[key] = value
    return out


def ready_for_rates(rfq, quote=None):
    """Whether this enquiry can be sent to a carrier at all."""
    return not validate(as_dict(rfq, quote))
