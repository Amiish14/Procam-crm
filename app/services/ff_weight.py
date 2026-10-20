"""
Chargeable weight: what the carrier actually bills.

FF-COMMS-01 R5. Freight is sold on whichever is greater, the space
or the mass, and quoting the wrong one is the commonest way a
forwarding job loses money before it starts.

Sea, LCL — weight or measure
    The revenue tonne is `max(volume in CBM, weight in tonnes)`.
    One cubic metre and one tonne are the same unit of sale, so a
    shipment of 8 CBM weighing 3 tonnes is billed as 8, and 2 CBM
    weighing 9 tonnes is billed as 9.

Air — volumetric at 1:6000
    `volumetric kg = L x W x H (cm) / 6000`, and the chargeable
    weight is the greater of that and the gross. IATA rounds up to
    the next half kilo, which is done here rather than left to the
    person: rounding down half a kilo on every line is a small loss
    taken a thousand times.

Rounding up, always
    Both modes round away from zero. A carrier does not round in our
    favour, so a quote built on a rounded-down figure is short by
    the difference on every shipment.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_CEILING

#: The air divisor. 6000 is the IATA volumetric factor for
#: centimetres; 5000 is used by some couriers and would change every
#: number here, so it is a parameter rather than a constant in a
#: formula.
AIR_DIVISOR_DEFAULT = 6000

#: IATA bills to the next half kilogramme.
AIR_STEP = Decimal('0.5')

#: Sea is quoted to three decimals of a revenue tonne.
SEA_STEP = Decimal('0.001')

SEA, AIR = 'Sea', 'Air'


class WeightError(ValueError):
    """A measurement that cannot be used as given."""


def _dec(value, label):
    if value in (None, ''):
        raise WeightError(f'{label} is required.')
    try:
        d = Decimal(str(value).replace(',', ''))
    except Exception:
        raise WeightError(f'{value!r} is not a number for {label}.')
    if d < 0:
        raise WeightError(f'{label} cannot be negative.')
    return d


def _round_up(value, step):
    if step <= 0:
        return value
    return (value / step).quantize(Decimal('1'),
                                   rounding=ROUND_CEILING) * step


def sea_chargeable(*, cbm, weight_kg):
    """Revenue tonnes for an LCL shipment: max(CBM, tonnes)."""
    volume = _dec(cbm, 'volume in CBM')
    kg = _dec(weight_kg, 'gross weight in kg')
    tonnes = kg / Decimal('1000')
    basis = 'volume' if volume >= tonnes else 'weight'
    chargeable = _round_up(max(volume, tonnes), SEA_STEP)
    return {
        'mode': SEA,
        'unit': 'W/M (revenue tonne)',
        'cbm': float(volume),
        'tonnes': float(tonnes),
        'chargeable': float(chargeable),
        'basis': basis,
        'explain': (f'{volume} CBM against {tonnes} tonnes — billed on '
                    f'{basis}, {chargeable} W/M.'),
    }


def air_chargeable(*, gross_kg, length_cm=None, width_cm=None,
                   height_cm=None, volume_cbm=None, pieces=1,
                   divisor=AIR_DIVISOR_DEFAULT):
    """Chargeable kilos: max(gross, volumetric at 1:6000).

    Dimensions or a volume — either, because a consolidation is
    quoted in cubic metres and a single crate in centimetres, and
    making somebody convert by hand is where the error enters.
    """
    gross = _dec(gross_kg, 'gross weight in kg')
    div = _dec(divisor, 'divisor')
    if div <= 0:
        raise WeightError('The volumetric divisor must be positive.')
    count = _dec(pieces or 1, 'pieces')
    if count <= 0:
        raise WeightError('Pieces must be at least one.')

    if volume_cbm not in (None, ''):
        cbm = _dec(volume_cbm, 'volume in CBM')
        # 1 CBM is 1,000,000 cubic centimetres.
        volumetric = cbm * Decimal('1000000') / div
        source = f'{cbm} CBM'
    elif None not in (length_cm, width_cm, height_cm) \
            and '' not in (length_cm, width_cm, height_cm):
        l = _dec(length_cm, 'length in cm')
        w = _dec(width_cm, 'width in cm')
        h = _dec(height_cm, 'height in cm')
        volumetric = (l * w * h * count) / div
        source = f'{l} x {w} x {h} cm x {count}'
    else:
        raise WeightError(
            'Give either the dimensions in centimetres or a volume in '
            'CBM — the volumetric weight cannot be worked out without '
            'one of them.')

    volumetric = _round_up(volumetric, AIR_STEP)
    gross_r = _round_up(gross, AIR_STEP)
    basis = 'volume' if volumetric >= gross_r else 'weight'
    chargeable = max(volumetric, gross_r)
    return {
        'mode': AIR,
        'unit': 'kg',
        'gross_kg': float(gross_r),
        'volumetric_kg': float(volumetric),
        'divisor': int(div),
        'chargeable': float(chargeable),
        'basis': basis,
        'explain': (f'{source} is {volumetric} kg volumetric against '
                    f'{gross_r} kg gross — billed on {basis}, '
                    f'{chargeable} kg.'),
    }


def chargeable(mode, **kw):
    """Dispatch on the lead's Mode. Multimodal has no single answer.

    A multimodal job is priced leg by leg — the sea leg on W/M and
    the air leg on volumetric — so returning one number for it would
    be inventing a figure no carrier would recognise.
    """
    m = (mode or '').strip().title()
    if m == SEA:
        return sea_chargeable(cbm=kw.get('cbm'),
                              weight_kg=kw.get('gross_kg'))
    if m == AIR:
        return air_chargeable(**{k: v for k, v in kw.items()
                                 if k != 'cbm'})
    raise WeightError(
        f'{mode!r} has no single chargeable weight. Sea is billed on '
        f'W/M and air on volumetric; a multimodal job is calculated '
        f'per leg, not as one figure.')
