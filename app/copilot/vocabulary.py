"""
§7 — the logistics vocabulary, and why it is a differentiator.

A generic assistant hears "ODC ex JNPT on hydraulic axles" as noise. It
is a customer telling you the cargo is over-dimensional, the origin is
Nhava Sheva, and the equipment is an SPMT-class trailer — which is a
Project Logistics enquiry, not a Sea Freight one.

Two jobs:

    expand(text)   →  synonyms folded in, so "quotations pending with
                      me", "pending quote" and "RFQ I have not quoted"
                      all reach the same intent
    vertical(text) →  the vertical the words point at, with the terms
                      that decided it

Nothing here is a classification on its own. `vertical()` returns its
reasoning so a person can disagree with it, and the existing
lead_vertical service still owns the recommendation on a lead — this is
the Copilot's reading of a *question*, which is a different thing.
"""
from __future__ import annotations

import re


#: §7's four verticals, and the words a Procam customer actually uses.
#: Weighted: a term that only ever means one vertical counts for more
#: than one that drifts.
VERTICAL_TERMS = {
    'Project Logistics': {
        3: ('odc', 'over dimensional', 'over-dimensional', 'breakbulk',
            'break bulk', 'hydraulic axle', 'spmt', 'rigging', 'jacking',
            'skidding', 'heavy lift', 'heavy-lift', 'project cargo',
            'module', 'reactor', 'transformer', 'stator', 'turbine',
            'crane barge', 'roro', 'ro-ro', 'lashing'),
        2: ('wind mill', 'windmill', 'nacelle', 'blade', 'boiler',
            'pressure vessel', 'girder', 'gantry'),
        1: ('project',),
    },
    # Named to match app/services/lead_vertical.py, which the intake
    # engine uses. This was "Heavy Transport" here and "Transportation"
    # there: two names for one service, and a question and an ingested
    # lead could come back labelled differently for the same words.
    'Transportation': {
        3: ('trailer', 'multi axle', 'multi-axle', 'low bed', 'lowbed',
            'semi low bed', 'puller', 'prime mover', 'axle line'),
        2: ('ftl', 'ptl', 'road transport', 'road freight', 'haulage',
            'truck', 'over weight', 'overweight', 'oversize'),
        1: ('transport', 'transportation', 'vehicle', 'movement',
            'multimodal', 'multi-modal', 'multi modal', 'intermodal'),
    },
    'Installation': {
        3: ('installation', 'erection', 'commissioning', 'mechanical '
            'completion', 'grouting', 'alignment and levelling',
            'hook-up', 'hook up'),
        2: ('foundation bolts', 'anchor bolts', 'site assembly',
            'placement on foundation'),
        1: ('installation site',),
    },
    'Chartering': {
        3: ('charter', 'chartering', 'charter party', 'voyage charter',
            'time charter', 'vessel charter', 'fixture note'),
        2: ('laytime', 'demurrage rate', 'part cargo'),
        1: (),
    },
    'Sea Freight': {
        3: ('fcl', 'lcl', 'bill of lading', 'b/l', 'shipping line',
            'vessel', 'ocean freight', 'container', 'teu', 'feu',
            'reefer', 'flat rack', 'open top', 'demurrage', 'detention'),
        2: ('port', 'jnpt', 'nhava sheva', 'mundra', 'kandla', 'chennai '
            'port', 'kattupalli', 'hazira', 'cochin', 'tuticorin',
            'krishnapatnam', 'sea freight', 'shipment by sea'),
        1: ('sea', 'marine', 'shipping'),
    },
    'Air Freight': {
        3: ('awb', 'air waybill', 'airfreight', 'air freight',
            'chargeable weight', 'volumetric weight', 'airline'),
        2: ('airport', 'air cargo', 'by air', 'express air'),
        1: ('air',),
    },
    'Customs': {
        3: ('cha', 'customs clearance', 'bill of entry', 'shipping bill',
            'boe', 'iec', 'hs code', 'hsn', 'duty drawback', 'igm',
            'let export', 'out of charge'),
        2: ('customs', 'clearance', 'bonded', 'warehouse bond',
            'ad code'),
        1: ('duty', 'tariff'),
    },
    'Warehousing': {
        3: ('warehouse', 'warehousing', 'pallet position', 'racking',
            'fulfilment', 'fulfillment', 'sqft storage', '3pl'),
        2: ('storage', 'inventory', 'stock keeping', 'pallet',
            'cross dock', 'cross-dock'),
        1: ('godown',),
    },
}

#: How each service above maps to the CRM's own Master Data service list
#: (scripts/2026_09_10_master_data.py: Heavy Transport, Project Freight,
#: Warehousing, Installation, Customs Clearance, Chartering). Derived from
#: that list, not invented. Sea Freight and Air Freight have no entry in
#: it, so they map to None and the glossary says so — which service they
#: sit under is a business decision, not something to guess here.
CRM_SERVICE = {
    'Project Logistics': 'Project Freight',
    'Transportation': 'Heavy Transport',
    'Warehousing': 'Warehousing',
    'Installation': 'Installation',
    'Customs': 'Customs Clearance',
    'Chartering': 'Chartering',
    'Sea Freight': None,
    'Air Freight': None,
}


def crm_service(name):
    return CRM_SERVICE.get(name)


#: §7's abbreviations, expanded so a question using either form matches.
ABBREVIATIONS = {
    'rfq': 'request for quotation',
    'rfi': 'request for information',
    'rfp': 'request for proposal',
    'boq': 'bill of quantities',
    'fcl': 'full container load',
    'lcl': 'less than container load',
    'fob': 'free on board',
    'cif': 'cost insurance freight',
    'exw': 'ex works',
    'dap': 'delivered at place',
    'bb': 'breakbulk',
    'fr': 'flat rack',
    'ot': 'open top',
    'hl': 'heavy lift',
    'odc': 'over dimensional cargo',
    'cha': 'customs house agent',
    'awb': 'air waybill',
    'bl': 'bill of lading',
    'spmt': 'self propelled modular transporter',
    'pod': 'proof of delivery',
    'eta': 'estimated time of arrival',
    'etd': 'estimated time of departure',
    'pic': 'person in charge',
    # This key was once typed with a Cyrillic 'а', so 'nba' never
    # matched it. Plain ASCII now.
    'nba': 'next best action',
    'oog': 'out of gauge',
    'ddp': 'delivered duty paid',
    'lolo': 'lift on lift off',
}

#: The same question, the way six different people type it. §6.3 asks
#: for synonym tolerance; this is where it lives, so the pattern list
#: does not have to carry every phrasing.
SYNONYMS = {
    'pending quote': ('quotations pending', 'quotation pending',
                      'quotes pending', 'awaiting quotation',
                      'yet to quote', 'not quoted yet',
                      'rfq i have not quoted', 'rfqs to quote'),
    # Deliberately NOT 'no follow up': §5 lists "no next action" as its
    # own question, and folding it here answered the wrong one.
    'stale': ('gone cold', 'no movement', 'not touched', 'sitting idle',
              'gone quiet', 'nothing happening'),
    'pipeline': ('funnel', 'open deals', 'live opportunities',
                 'what is in play'),
    'who handles': ('who is handling', 'whose account', 'account owner',
                    'kaun dekh raha hai', 'who is looking after'),
    'my day': ('kya karna hai', 'what is on my plate', 'my tasks today',
               'todays work', "today's work"),
    'lost': ('lost business', 'we lost', 'did not win', 'gone to a '
             'competitor'),
}

_WORD = re.compile(r"[a-z0-9/&'-]+")


def expand(text):
    """Normalise a question so one pattern matches many phrasings.

    Both synonyms and abbreviations are APPENDED, never substituted.
    Replacing looked tidier and was wrong: "largest open deals" became
    "largest pipeline" because "open deals" is a synonym for pipeline,
    and the question stopped matching the intent it plainly meant.
    Appending is lossless — the original words still match their own
    patterns, and the canonical form matches too.
    """
    s = ' ' + (text or '').lower().strip() + ' '
    extra = []
    for canonical, variants in SYNONYMS.items():
        for v in variants:
            if v in s:
                extra.append(canonical)
                break
    for token in _WORD.findall(s):
        long_form = ABBREVIATIONS.get(token)
        if long_form:
            extra.append(long_form)
    if extra:
        s = s + ' ' + ' '.join(extra) + ' '
    return s.strip()


def vertical(text):
    """(vertical, confidence, why) — or (None, 0, []) when unclear.

    Confidence comes from the margin between the top two, not the raw
    score: "container" alone should not read as 90% Sea Freight when
    the sentence also says "hydraulic axle".
    """
    s = ' ' + (text or '').lower() + ' '
    scores, hits = {}, {}
    for vert, weighted in VERTICAL_TERMS.items():
        total, found = 0, []
        for weight, terms in weighted.items():
            for term in terms:
                if term in s:
                    total += weight
                    found.append(term)
        if total:
            scores[vert] = total
            hits[vert] = found
    if not scores:
        return None, 0, []

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top, top_score = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0
    margin = (top_score - second) / float(top_score)
    confidence = int(round(50 + 50 * margin))
    return top, confidence, hits[top]


def glossary():
    """Every term the Copilot understands, for the help screen."""
    out = []
    for vert, weighted in VERTICAL_TERMS.items():
        terms = sorted({t for group in weighted.values() for t in group})
        out.append({'vertical': vert, 'terms': terms,
                    'crm_service': crm_service(vert),
                    'note': (None if crm_service(vert) else
                             'Not in the CRM service master — which service '
                             'this belongs under is a business decision.')})
    return {'verticals': out,
            'abbreviations': dict(sorted(ABBREVIATIONS.items()))}


# ── §4 retrieval: the same thing, written the ways a desk writes it ──
#
# Kept apart from SYNONYMS on purpose. SYNONYMS folds a *question* onto
# an intent; these widen a *text search*, where "B/L" in a query has to
# find "bill of lading" in an email. Mixing them would let a word in a
# customer's email change which intent a question routes to.
#
# Each group is a set of equivalent spellings. Order does not matter.
# Short forms are matched on word boundaries by the ranker, never as
# substrings — "bl" must not score "table".
RETRIEVAL_SYNONYMS = (
    ('odc', 'over dimensional cargo', 'over-dimensional',
     'over dimensional', 'oversize cargo'),
    ('oog', 'out of gauge', 'out-of-gauge'),
    ('cha', 'customs house agent', 'customs broker', 'custom house agent'),
    ('b/l', 'bl', 'bill of lading', 'hbl', 'mbl'),
    ('eta', 'estimated time of arrival'),
    ('etd', 'estimated time of departure'),
    ('hs code', 'hsn', 'hsn code', 'harmonized system code'),
    ('fcl', 'full container load'),
    ('lcl', 'less than container load'),
    ('roro', 'ro-ro', 'ro ro', 'roll on roll off'),
    ('breakbulk', 'break bulk', 'break-bulk'),
    ('project cargo', 'project shipment'),
    ('heavy lift', 'heavy-lift', 'heavylift'),
    ('reefer', 'refrigerated container', 'reefer container'),
    ('demurrage', 'detention', 'port storage charges'),
    ('ex-works', 'ex works', 'exw'),
    ('fob', 'free on board'),
    ('cif', 'cost insurance freight', 'cost insurance and freight'),
    ('dap', 'delivered at place'),
    ('ddp', 'delivered duty paid'),
    ('lolo', 'lo-lo', 'lift on lift off'),
    ('awb', 'air waybill', 'airway bill'),
    ('spmt', 'self propelled modular transporter'),
    ('boq', 'bill of quantities'),
)


def _norm(text):
    return ' '.join((text or '').lower().replace('_', ' ').split())


def _has_phrase(haystack, phrase):
    """Word-boundary containment: "cha" is in "a cha quote", not "chat"."""
    return re.search(r'(?<![a-z0-9])' + re.escape(phrase) + r'(?![a-z0-9])',
                     haystack) is not None


def expansion_groups(query):
    """For each synonym group the query touches, the spellings it did
    NOT already use — one list per concept.

    Grouped so the ranker can count a concept once however many of its
    spellings a passage carries, and weight all of them below the words
    the person actually typed: an expansion is a hint, the typed word is
    evidence.
    """
    q = ' ' + _norm(query) + ' '
    out = []
    for group in RETRIEVAL_SYNONYMS:
        present = [g for g in group if _has_phrase(q, g)]
        if not present:
            continue
        alts = [g for g in group if g not in present]
        if alts:
            out.append(alts)
    return out


#: Every one- or two-letter spelling the synonym groups know ("bl").
_SHORT_FORMS = {g for group in RETRIEVAL_SYNONYMS for g in group
                if len(g) <= 2}


def short_forms(query):
    """The known short abbreviations the query types, in order.

    The retrieval tokenizer drops words under three letters so that "of"
    and "to" rank nothing — which also dropped "BL". Only spellings in
    RETRIEVAL_SYNONYMS come back, so "of" still does not."""
    out = []
    for word in re.findall(r"[a-z0-9/]+", _norm(query)):
        if word in _SHORT_FORMS and word not in out:
            out.append(word)
    return out


def retrieval_expansions(query):
    """expansion_groups, flattened."""
    out = []
    for group in expansion_groups(query):
        for g in group:
            if g not in out:
                out.append(g)
    return out


# ── the CRM's services, however a record spells them ─────────────────
#
# Lead.procam_vertical is written by the intake engine ("Project
# Logistics", "Transportation"); Company.vertical and Employee.vertical
# by people and the service master ("Project Freight", "Heavy
# Transport", sometimes "PFM"). A filter on one spelling silently misses
# the rows carrying the other, so a filter always matches the whole
# alias set of the service asked for.
SERVICE_ALIASES = {
    'Project Freight': ('Project Freight', 'Project Logistics', 'PFM'),
    'Heavy Transport': ('Heavy Transport', 'Transportation'),
    'Warehousing': ('Warehousing',),
    'Installation': ('Installation',),
    'Customs Clearance': ('Customs Clearance', 'Customs'),
    'Chartering': ('Chartering',),
    'Sea Freight': ('Sea Freight',),
    'Air Freight': ('Air Freight',),
}

#: What a person types when they mean one of the services above. Longest
#: phrases first, so "project freight" wins over anything shorter.
_SERVICE_WORDS = sorted([
    ('project freight', 'Project Freight'),
    ('project logistics', 'Project Freight'),
    ('project cargo', 'Project Freight'),
    ('pfm', 'Project Freight'),
    ('heavy transport', 'Heavy Transport'),
    ('transportation', 'Heavy Transport'),
    ('warehousing', 'Warehousing'),
    ('installation', 'Installation'),
    ('customs clearance', 'Customs Clearance'),
    ('customs', 'Customs Clearance'),
    ('chartering', 'Chartering'),
    ('sea freight', 'Sea Freight'),
    ('ocean freight', 'Sea Freight'),
    ('air freight', 'Air Freight'),
], key=lambda kv: -len(kv[0]))


def service_in_text(text):
    """The CRM service a phrase names ("only Project Freight"), or None.

    Deliberately literal. `vertical()` reads cargo descriptions and
    weighs evidence; this reads a filter somebody typed, where a guess
    would narrow an answer to a service they did not ask for.
    """
    s = ' ' + _norm(text) + ' '
    for phrase, service in _SERVICE_WORDS:
        if _has_phrase(s, phrase):
            return service
    return None


def service_aliases(service):
    """Every spelling of one service, for an IN (...) filter."""
    if not service:
        return ()
    return SERVICE_ALIASES.get(service, (service,))


def crm_services():
    """The services Procam sells, named as the service master names them.

    Sea and Air Freight are left out for the reason CRM_SERVICE gives:
    which master service they belong under is a business decision."""
    return [s for s in CRM_SERVICE.values() if s]
