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


#: Where a vertical came from, as a word rather than a sentence.
#:
#: CRM-FINAL-13NOV item 11. A heavy-transport tender was filed under
#: Warehousing, and `vertical_reason` already said why in words -- but
#: a sentence cannot be branched on, and the rule the item asks for is
#: a branch: do not route a lead to an owner on the strength of a
#: guess. So the same answer is also recorded as one of these.
STATED = 'stated'          # the mail or the form said so
ACCOUNT = 'account'        # the customer's own Account Master vertical
GUESSED = 'guessed'        # read off the words in the text
ATTACHMENT = 'attachment'  # read off a document attached to it
NONE = 'none'

SOURCE_LABELS = {
    STATED: 'stated on the enquiry',
    ACCOUNT: "the customer's own vertical",
    GUESSED: 'guessed from the wording',
    ATTACHMENT: 'read from an attachment',
    NONE: 'not established',
}

#: At or above this, a guess is treated as a call. Below it the lead
#: is a "needs review" one and nothing routes on it.
FIRM_ENOUGH = 80


def source_for(why, confidence=None, *, from_attachment=False):
    """One of the codes above, from what `recommend` answered.

    Read off the sentence rather than returned as a fourth value,
    because `recommend` has callers and a changed signature is a
    change to all of them for a fact they do not use.
    """
    text = (why or '').strip().lower()
    if from_attachment:
        return ATTACHMENT
    if not text:
        return NONE
    if 'account master' in text or 'account' in text:
        return ACCOUNT
    if 'nothing in the text' in text:
        return NONE
    return GUESSED


def is_firm(confidence, source=None):
    """Whether a lead may be routed on this vertical.

    A stated or account-derived vertical is firm at any confidence --
    somebody said so. A guess is firm only at FIRM_ENOUGH, and that is
    the number item 11 is about: the Warehousing call on a heavy
    transport tender was a guess that nothing refused to act on.
    """
    if source in (STATED, ACCOUNT, ATTACHMENT):
        return True
    try:
        return int(confidence or 0) >= FIRM_ENOUGH
    except (TypeError, ValueError):
        return False


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
