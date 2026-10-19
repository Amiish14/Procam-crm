"""
How the freight moves: Sea, Air, or Multimodal.

Only Freight Forwarding asks. Until 18 October the vertical itself
carried the answer — Sea Freight and Air Freight were verticals, so a
forwarding job had to be filed as one or the other and a job that was
both had nowhere to go. CRM-VERTICAL-MASTER-01 folded the pair into
Freight Forwarding, which is right: it is one team, one head and one
P&L. The distinction between sea and air is not an organisational
one and should never have been a vertical; it is a property of the
shipment, so it lives on the shipment.

Optional everywhere. A blank mode means nobody has said, not that the
job is neither — the backfill only fills in what the audit trail
actually records and leaves the rest alone.
"""
from __future__ import annotations

#: The vertical this field belongs to. Anything else does not show it
#: and is not filtered by it.
VERTICAL = 'Freight Forwarding'

#: The whole vocabulary, in the order a dropdown lists it.
MODES = ('Sea', 'Air', 'Multimodal')

#: What the old verticals meant, for the backfill and for anyone
#: reading a historic export. Multimodal has no old equivalent: the
#: vertical pair could not express it.
FROM_VERTICAL = {
    'sea freight': 'Sea',
    'air freight': 'Air',
}

_BY_LOWER = {m.lower(): m for m in MODES}

#: Spellings people type or import that mean one of the three.
_ALIASES = {
    'ocean': 'Sea', 'sea freight': 'Sea', 'by sea': 'Sea', 'fcl': 'Sea',
    'lcl': 'Sea', 'air freight': 'Air', 'by air': 'Air',
    'multi-modal': 'Multimodal', 'multi modal': 'Multimodal',
    'sea+air': 'Multimodal', 'sea & air': 'Multimodal',
    'air+sea': 'Multimodal', 'combined': 'Multimodal',
}


def normalise(value):
    """A stored mode, or None.

    Returns None for anything unrecognised rather than guessing. A
    field that quietly turns a typo into 'Sea' is worse than a field
    that stays blank: the blank is visible.
    """
    probe = (value or '').strip().lower()
    if not probe:
        return None
    return _BY_LOWER.get(probe) or _ALIASES.get(probe)


def applies_to(vertical):
    """Whether a lead in this vertical is asked for a mode."""
    return (vertical or '').strip().lower() == VERTICAL.lower()


def from_vertical(old_vertical):
    """The mode the old vertical name implied, or None."""
    return FROM_VERTICAL.get((old_vertical or '').strip().lower())
