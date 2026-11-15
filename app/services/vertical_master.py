"""One spelling per vertical.

CRM-SEARCH-SCAN-01 §3. The live book holds thirteen spellings of ten
desks -- "Heavy transport", "HEAVY TRANSPORT", "Project freight",
"PFM" -- and a filter cannot AND across spellings. A person choosing
"Heavy Transport" from a list and getting two thirds of the heavy
transport leads is worse than no filter: the answer looks complete.

So there is one list, and everything that reads a vertical reads it
through `canonical`.

WHY A RULE AND NOT A TABLE OF THIRTEEN

A table of what is in the data today is out of date the first time
somebody types a fourteenth. The rule normalises -- case, spacing,
punctuation, "and" for "&" -- and then matches against the master
list and a small set of names the business actually uses for the same
desk ("PFM" is Project Freight Management; "Customs Clearance" is
Customs). Anything it cannot place is returned UNMAPPED rather than
guessed at, and the sweep that rewrites the data lists those for a
person instead of filing them under "Other".
"""
from __future__ import annotations

import re

#: The ten, as agreed on 15 Nov. Order is display order.
CANONICAL = (
    'Heavy Transport',
    'Project Freight',
    'Installation',
    'Warehousing',
    'Freight Forwarding',
    'Sea Freight',
    'Air Freight',
    'Customs',
    'Heavy Cargo',
    'Other',
)

#: The master-data list these belong to. `master_data.USAGE` already
#: points this key at `Lead.procam_vertical` and `Employee.vertical`.
LIST_KEY = 'vertical'

#: What a desk is also called. Keys are normalised; values are one of
#: CANONICAL. Only names the business uses -- an alias nobody says is
#: a guess waiting to be wrong.
ALIASES = {
    'ht': 'Heavy Transport',
    'heavy transportation': 'Heavy Transport',
    # Both named on 15 Nov. 'General Transport' was an Other; the rest
    # of the CRM has read it as Heavy Transport since the vertical
    # master went in (vertical_master_apply.py, vertical_heads), so
    # filing it under Other here was the odd one out.
    'transportation': 'Heavy Transport',
    'general transport': 'Heavy Transport',
    'general transportation': 'Heavy Transport',
    'heavy haulage': 'Heavy Transport',
    'odc': 'Heavy Transport',
    'pfm': 'Project Freight',
    'project freight management': 'Project Freight',
    'projects': 'Project Freight',
    'project cargo': 'Project Freight',
    'project logistics': 'Project Freight',
    'erection': 'Installation',
    'erection and commissioning': 'Installation',
    'installation and commissioning': 'Installation',
    'commissioning': 'Installation',
    'warehouse': 'Warehousing',
    'warehousing and distribution': 'Warehousing',
    'ff': 'Freight Forwarding',
    'forwarding': 'Freight Forwarding',
    'freight forwarder': 'Freight Forwarding',
    'ocean freight': 'Sea Freight',
    'sea': 'Sea Freight',
    'ocean': 'Sea Freight',
    'air': 'Air Freight',
    'airfreight': 'Air Freight',
    'customs clearance': 'Customs',
    'cha': 'Customs',
    'clearance': 'Customs',
    'custom': 'Customs',
    'heavy lift': 'Heavy Cargo',
    'heavylift': 'Heavy Cargo',
    'others': 'Other',
    'general': 'Other',
    'misc': 'Other',
}

#: What `canonical` answers when it will not guess.
UNMAPPED = ''


def norm(value):
    """Lower case, no punctuation, single spaces, "and" for "&"."""
    text = str(value or '').strip().lower()
    if not text:
        return ''
    text = text.replace('&', ' and ')
    text = re.sub(r'[^a-z0-9]+', ' ', text)
    return ' '.join(text.split())


_BY_NORM = {norm(name): name for name in CANONICAL}


def canonical(value):
    """The one spelling of this desk, or '' when it cannot be placed.

    '' rather than 'Other': "I do not know which desk this is" and
    "this is the Other desk" are different answers, and a sweep that
    files the first under the second loses the question.
    """
    key = norm(value)
    if not key:
        return ''
    if key in _BY_NORM:
        return _BY_NORM[key]
    if key in ALIASES:
        return ALIASES[key]
    # "Heavy Transport - North", "Air Freight (Export)": the desk with
    # something after it. Longest canonical name first, so "Heavy
    # Cargo" does not win a string that says "Heavy Cargo Handling"
    # when a longer name also matches.
    for name in sorted(CANONICAL, key=len, reverse=True):
        base = norm(name)
        if key.startswith(base + ' ') or key.endswith(' ' + base):
            return name
    for alias in sorted(ALIASES, key=len, reverse=True):
        if len(alias) < 4:
            continue            # "ht", "ff", "cha" only match whole
        if key.startswith(alias + ' ') or key.endswith(' ' + alias):
            return ALIASES[alias]
    return UNMAPPED


def is_canonical(value):
    return (value or '') in CANONICAL


def seed(actor=None):
    """Put the ten into Admin → Master data. Returns what it added.

    Idempotent: `add_item` reactivates a retired value rather than
    colliding, and an item already there is left as it is.
    """
    from app.master_data import service as md

    added = []
    try:
        held = {i.code for i in md.items(LIST_KEY, include_inactive=True)}
    except Exception:                                     # noqa: BLE001
        held = set()
    for order, name in enumerate(CANONICAL):
        if name in held:
            continue
        try:
            md.add_item(LIST_KEY, name, label=name,
                        sort_order=order * 10, actor=actor)
            added.append(name)
        except Exception:                                 # noqa: BLE001
            continue
    return added
