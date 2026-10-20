"""
Charge groups, and which of them the Incoterm puts on our client.

FF-COMMS-01. A forwarding quote is not one number; it is a stack of
charges that belong to different parties depending on the Incoterm
agreed between shipper and consignee. Tagging every sell line with a
charge group is what lets the quote answer "is this ours to bill?"
without somebody re-reading the contract each time.

The matrix below is Incoterms 2020, expressed as *the groups the
seller bears*. The buyer bears the rest; there is no third option, so
one list defines both and they cannot drift apart.

A caution worth keeping in view: an Incoterm allocates cost and risk
between **shipper and consignee**. It does not, on its own, say what
Procam bills — that depends on which of the two is our client, which
is why `billable_groups` takes `party`. A forwarder who reads the
Incoterm without asking whose side they are on bills the wrong half.
"""
from __future__ import annotations

# ── the groups ───────────────────────────────────────────────────────
#: key, label, what belongs in it. Ordered as they appear on a quote,
#: origin first and delivery last, because that is the order the
#: cargo meets them and the order a client reads them in.
CHARGE_GROUPS = (
    ('origin',          'Origin charges',
     'Pickup, origin handling, origin documentation, terminal.'),
    ('export_customs',  'Export customs',
     'Export declaration, inspection, permits at origin.'),
    ('freight',         'Freight',
     'Main carriage: ocean, air or the long-haul leg.'),
    ('insurance',       'Insurance',
     'Marine or air cargo insurance on the consignment.'),
    ('import_customs',  'Import customs',
     'Import declaration, examination, CHA handling at destination.'),
    ('duties',          'Duties and taxes',
     'Customs duty, IGST and other statutory levies.'),
    ('destination',     'Destination charges',
     'Destination terminal handling, unloading, demurrage.'),
    ('delivery',        'Delivery',
     'On-carriage to the consignee door.'),
    # ── beyond forwarding (CRM 2.0 R5) ───────────────────────────
    # The same idea in the other verticals: a quote is a stack of
    # charges and the client is entitled to see which is which. The
    # keys stay distinct from the forwarding ones even where the
    # work rhymes — "pickup" on a road job is not "origin charges"
    # on a shipment, and merging them would make one report that is
    # wrong about both.
    ('mobilisation',    'Mobilisation',
     'Bringing equipment, crew and gear to site.'),
    ('line_haul',       'Line haul',
     'The road or rail movement itself.'),
    ('escort_permits',  'Escorts and permits',
     'Pilot vehicles, route survey, state permits, police escort.'),
    ('handling',        'Loading and unloading',
     'Cranage, rigging, lashing and unlashing at either end.'),
    ('equipment',       'Equipment hire',
     'Cranes, trailers, jacks, SPMTs and their operators.'),
    ('labour',          'Labour',
     'Crew, supervision and site manpower.'),
    ('storage',         'Storage',
     'Warehouse space, by pallet, square foot or month.'),
    ('value_added',     'Value-added services',
     'Kitting, packing, labelling, stock counts, reporting.'),
    ('demobilisation',  'Demobilisation',
     'Clearing site and returning equipment.'),
    ('waiting',         'Waiting and detention',
     'Time at site beyond the free period.'),
    ('other',           'Other',
     'Anything the groups above do not describe.'),
)

GROUP_KEYS = tuple(k for k, _l, _d in CHARGE_GROUPS)
GROUP_LABEL = {k: l for k, l, _d in CHARGE_GROUPS}

# ── Incoterms 2020 ───────────────────────────────────────────────────
#: Groups the **seller** bears under each term. The buyer bears the
#: complement. `duties` sits with the buyer everywhere but DDP, which
#: is the whole point of DDP.
SELLER_BEARS = {
    'EXW': (),
    'FCA': ('origin', 'export_customs'),
    'FAS': ('origin', 'export_customs'),
    'FOB': ('origin', 'export_customs'),
    'CFR': ('origin', 'export_customs', 'freight'),
    'CIF': ('origin', 'export_customs', 'freight', 'insurance'),
    'CPT': ('origin', 'export_customs', 'freight'),
    'CIP': ('origin', 'export_customs', 'freight', 'insurance'),
    'DAP': ('origin', 'export_customs', 'freight', 'destination'),
    'DPU': ('origin', 'export_customs', 'freight', 'destination'),
    'DDP': ('origin', 'export_customs', 'freight', 'destination',
            'delivery', 'import_customs', 'duties'),
}

INCOTERMS = tuple(SELLER_BEARS)

#: Who our client is on the shipment. Not a detail: the same Incoterm
#: bills the opposite half depending on the answer.
PARTIES = ('Shipper', 'Consignee')


class ChargeError(ValueError):
    """A group or term the system does not recognise."""


#: Which groups belong on a quote in each vertical. A list per
#: vertical rather than one list for everything: an installation
#: quote offering "Export customs" is a quote the client stops
#: reading, and a transport quote with no "Escorts and permits"
#: line loses the argument about who pays for the pilot vehicle.
#:
#: `other` is on every list, because the charge nobody anticipated
#: is the one that costs money.
VERTICAL_GROUPS = {
    'Freight Forwarding': (
        'origin', 'export_customs', 'freight', 'insurance',
        'import_customs', 'duties', 'destination', 'delivery',
        'other'),
    'Project Freight': (
        'origin', 'export_customs', 'freight', 'insurance',
        'import_customs', 'duties', 'destination', 'delivery',
        'escort_permits', 'handling', 'equipment', 'other'),
    'General Transport': (
        'mobilisation', 'line_haul', 'handling', 'escort_permits',
        'waiting', 'other'),
    'Heavy Transport': (
        'mobilisation', 'line_haul', 'escort_permits', 'equipment',
        'handling', 'labour', 'waiting', 'demobilisation', 'other'),
    'Installation': (
        'mobilisation', 'equipment', 'labour', 'handling',
        'demobilisation', 'waiting', 'other'),
    'Warehousing': (
        'handling', 'storage', 'value_added', 'delivery', 'waiting',
        'other'),
}

#: Verticals where an Incoterm decides who is billed. An Incoterm is
#: a term of international sale; a lorry from Pune to Nashik has no
#: Incoterm, and asking for one would be a field nobody can fill.
INCOTERM_VERTICALS = ('Freight Forwarding', 'Project Freight')

DEFAULT_VERTICAL = 'Freight Forwarding'


def groups_for(vertical):
    """The charge groups a quote in this vertical may carry."""
    key = (vertical or '').strip()
    return VERTICAL_GROUPS.get(key, VERTICAL_GROUPS[DEFAULT_VERTICAL])


def uses_incoterms(vertical):
    return (vertical or '').strip() in INCOTERM_VERTICALS


def is_group(key):
    return key in GROUP_KEYS


def is_group_for(key, vertical):
    return key in groups_for(vertical)


def billable_groups(incoterm, party, vertical=None):
    """The charge groups Procam bills its client, as a set.

    `party` is which side of the shipment the client is on. A
    shipper-client is billed what the seller bears; a
    consignee-client what the buyer bears.

    Outside the forwarding verticals there is no Incoterm to consult
    and no second party to carry half the charges: our client is the
    only party, so everything on the quote is theirs. Returning the
    whole set is the honest answer there, not a default standing in
    for a rule nobody wrote.
    """
    if vertical is not None and not uses_incoterms(vertical):
        return set(groups_for(vertical))
    term = (incoterm or '').strip().upper()
    if term not in SELLER_BEARS:
        raise ChargeError(
            f'{incoterm!r} is not an Incoterms 2020 term. '
            f'Expected one of: {", ".join(INCOTERMS)}.')
    who = (party or '').strip().title()
    if who not in PARTIES:
        raise ChargeError(
            f'{party!r} is neither Shipper nor Consignee, and the same '
            f'Incoterm bills the opposite half depending on which.')
    seller = set(SELLER_BEARS[term])
    if who == 'Shipper':
        return seller
    return set(GROUP_KEYS) - seller - {'other'} | {'other'}


def is_billable(group, incoterm, party, vertical=None):
    """Whether one sell line falls to our client under this term."""
    if not is_group(group):
        raise ChargeError(f'{group!r} is not a charge group.')
    if vertical is not None and not uses_incoterms(vertical):
        return True
    # `other` is always billable: it exists for the charges no term
    # anticipated, and dropping them silently because a matrix has no
    # row for them is how a quote loses money.
    if group == 'other':
        return True
    return group in billable_groups(incoterm, party)


def split_lines(lines, incoterm, party, vertical=None):
    """(billable, not_billable) for lines carrying `charge_group`.

    Lines that are not billable are *kept and marked*, never dropped:
    a forwarder quoting DDP still has to see the duty line, and a
    quote that silently omits what the other party pays is how a
    scope argument starts three months later.
    """
    yes, no = [], []
    for ln in lines:
        group = getattr(ln, 'charge_group', None) or 'other'
        (yes if is_billable(group, incoterm, party, vertical)
         else no).append(ln)
    return yes, no
