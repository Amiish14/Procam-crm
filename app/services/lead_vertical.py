"""
Which Procam desk should handle this enquiry — §17.

A recommendation, never a decision. The Account Master wins wherever it
is set, because who handles Tata Steel is a relationship, not a keyword
match. This only fills the gap for an account nobody has classified, and
it always shows its confidence so a person can overrule it.

Pure: no database, no app import, so the keyword sets can be exercised
directly.
"""
from __future__ import annotations

import re

#: vertical → (weight, pattern). Weights differ because some words are
#: decisive and others merely suggestive: "hydraulic axle" is only ever
#: project cargo, while "container" appears in half of all logistics mail.
VERTICAL_SIGNALS = {
    'Project Logistics': [
        (10, r'\b(odc|over[\s-]?dimensional|heavy[\s-]?lift|breakbulk|'
             r'break[\s-]?bulk|hydraulic axle|axle pull\w*|spmt|'
             r'project cargo|module|reactor|transformer|turbine|'
             r'crane girder|windmill|nacelle|boiler)\b'),
        (4, r'\b(rigging|jacking|skidding|lashing|heavy equipment|'
            r'plant shifting|erection)\b'),
    ],
    'Sea Freight': [
        (10, r'\b(fcl|lcl|ocean freight|sea freight|bill of lading|'
             r'shipping line|vessel|feeder|transshipment|cbm)\b'),
        (4, r'\b(container|teu|feu|20ft|40ft|40hc|port of loading|pol|pod)\b'),
    ],
    'Air Freight': [
        (10, r'\b(awb|air ?waybill|air freight|airfreight|air shipment|'
             r'air cargo|chargeable weight)\b'),
        (4, r'\b(airport|airline|by air|iata)\b'),
    ],
    'Customs': [
        (10, r'\b(cha|customs clearance|bill of entry|shipping bill|'
             r'customs broker|icegate|duty drawback|hs code)\b'),
        (4, r'\b(clearance|customs|dgft|igm)\b'),
    ],
    'Warehousing': [
        (10, r'\b(warehous\w*|storage|3pl|fulfil\w*|pallet|racking|'
             r'inventory|bonded warehouse|cfs)\b'),
        (4, r'\b(sqft|square feet|stock|put[\s-]?away)\b'),
    ],
    'Transportation': [
        (10, r'\b(ftl|ptl|trailer|truck|lorry|road freight|'
             r'road transport\w*|tipper|tanker|flatbed)\b'),
        (4, r'\b(vehicle|fleet|multi[\s-]?axle|dispatch)\b'),
    ],
}


def recommend(text, *, account_vertical=None):
    """(vertical, confidence, why).

    `account_vertical` wins unless the text points somewhere else
    decisively — a warehousing client can still send a road-transport
    enquiry, and the Account Master should not silently mislabel it.
    """
    low = (text or '').lower()
    scores, hits = {}, {}
    for vertical, patterns in VERTICAL_SIGNALS.items():
        total, matched = 0, []
        for weight, pattern in patterns:
            found = re.findall(pattern, low)
            if found:
                total += weight * min(len(found), 3)
                matched.extend(sorted({f if isinstance(f, str) else f[0]
                                       for f in found})[:4])
        if total:
            scores[vertical] = total
            hits[vertical] = matched[:6]

    if not scores:
        if account_vertical:
            return account_vertical, 60, 'from the Account Master'
        return None, 0, 'nothing in the text points anywhere'

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top, top_score = ranked[0]
    runner = ranked[1][1] if len(ranked) > 1 else 0

    # Confidence is about the margin, not the raw score: two verticals
    # scoring 20 each is a coin toss however strong each looks alone.
    margin = (top_score - runner) / float(top_score)
    confidence = int(min(95, 45 + 50 * margin))
    why = 'matched ' + ', '.join(hits[top][:4])

    if account_vertical and account_vertical != top:
        # The Account Master is the default; only a clear signal moves it.
        if top_score < 10 or margin < 0.5:
            return account_vertical, 70, (
                f'Account Master says {account_vertical}; the text leans '
                f'{top} but not clearly')
        return top, confidence, (
            f'{why} — overriding the Account Master ({account_vertical})')

    return top, confidence, why
