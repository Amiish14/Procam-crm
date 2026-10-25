"""
A revision keeps the first quote's number.

CRM-QUOTE-REV-02 (R3), D2, behind `FEATURE_QUOTE_REV_V2`.

Today each revision takes a brand-new `QUO-YYYY-NNNN`. The
customer who received `QUO-2026-0045` is then sent
`QUO-2026-0061` and has to be told they are the same quotation —
and the pair is only discoverable by walking `parent_quote_id`,
which nobody does in a meeting.

So the display number becomes `QUO-2026-0045 / Rev-01` for every
revision in the chain. The internal `quote_number` is left alone:
it is unique, it is on 0 rows today but will be on every row
tomorrow, and changing what a stored identifier means is how you
lose the ability to find anything later.
"""
from __future__ import annotations

FLAG = 'FEATURE_QUOTE_REV_V2'

#: §8 — why a revision exists.
REVISION_REASONS = (
    'Customer asked for a revised price',
    'Scope changed',
    'Quantity changed',
    'Validity expired',
    'Error corrected',
    'Others',
)

#: Topics two conditions can disagree about. A conflict is the
#: same topic carrying different numbers — "validity 30 days" and
#: "validity 45 days" in one quotation is not a detail, it is a
#: dispute waiting to be discovered by the customer.
CONFLICT_TOPICS = {
    'validity': ('validity', 'valid for', 'valid until', 'quote valid'),
    'payment': ('payment term', 'days credit', 'advance', 'net '),
    'free time': ('free time', 'detention', 'demurrage', 'free days'),
}


def enabled(emp_code=None):
    from flask import has_request_context, session

    from app.services import flags

    if emp_code is None and has_request_context():
        return flags.on_for(FLAG, session.get('emp_code'))
    if emp_code is None:
        return flags.on(FLAG)
    return flags.on_for(FLAG, emp_code)


# ── the number a person sees ─────────────────────────────────────────
def root_of(quote):
    """The first quote in this chain. Never None for a saved row."""
    from app import db
    from app.models.quote import Quote

    if quote is None:
        return None
    if quote.root_quote_id and quote.root_quote_id != quote.id:
        found = db.session.get(Quote, quote.root_quote_id)
        if found is not None:
            return found
    if quote.root_quote_id == quote.id:
        return quote
    # Nothing recorded: walk the parents, which is what the
    # backfill does and what an un-backfilled row needs.
    seen, node = {quote.id}, quote
    while node.parent_quote_id and node.parent_quote_id not in seen:
        seen.add(node.parent_quote_id)
        parent = db.session.get(Quote, node.parent_quote_id)
        if parent is None:
            break
        node = parent
    return node


def display_number(quote):
    """`QUO-2026-0045 / Rev-01`, which is what a customer sees."""
    if quote is None:
        return ''
    if not enabled():
        return quote.quote_number or ''
    root = root_of(quote)
    number = (root.quote_number if root is not None
              else quote.quote_number) or ''
    rev = quote.rev_no
    if rev is None:
        rev = max(0, (quote.revision_number or 1) - 1)
    return f'{number} / Rev-{int(rev):02d}'


def next_rev_no(root):
    """One more than the highest revision in this chain."""
    from app.models.quote import Quote

    if root is None:
        return 0
    highest = 0
    for row in Quote.query.filter(
            (Quote.root_quote_id == root.id) | (Quote.id == root.id)
            ).all():
        rev = row.rev_no
        if rev is None:
            rev = max(0, (row.revision_number or 1) - 1)
        highest = max(highest, int(rev))
    return highest + 1


def chain(quote):
    """Every revision of this quotation, oldest first."""
    from app.models.quote import Quote

    root = root_of(quote)
    if root is None:
        return []
    rows = Quote.query.filter(
        (Quote.root_quote_id == root.id) | (Quote.id == root.id)).all()
    return sorted(rows, key=lambda q: (q.rev_no if q.rev_no
                                       is not None else 0, q.id))


def mark_superseded(quote, *, actor):
    """On sending a revision, the one before it is no longer live."""
    from app.services import audit

    changed = []
    for row in chain(quote):
        if row.id == quote.id:
            continue
        if (row.status or '') in ('Superseded', 'Won', 'Lost'):
            continue
        was = row.status
        row.status = 'Superseded'
        audit.record('quote.superseded', 'quote', row.id,
                     old={'status': was}, new={'status': 'Superseded'},
                     actor=actor,
                     reason=f'{display_number(quote)} was sent')
        changed.append(row.id)
    return changed


# ── conditions flowing into the quote (§5.2) ─────────────────────────
def _norm(text):
    import re
    return re.sub(r'\s+', ' ', (text or '').strip().lower())


def topic_of(text):
    low = _norm(text)
    for topic, needles in CONFLICT_TOPICS.items():
        if any(n in low for n in needles):
            return topic
    return ''


def _numbers(text):
    import re
    return tuple(re.findall(r'\d+', text or ''))


def conflicts(texts):
    """[(topic, a, b)] where one quotation says two things.

    Same topic, different numbers. Two conditions that merely
    mention validity are not a conflict; two that name different
    validity periods are, and that is the pair a customer finds.
    """
    out, by_topic = [], {}
    for text in texts:
        topic = topic_of(text)
        if not topic:
            continue
        nums = _numbers(text)
        if not nums:
            continue
        prior = by_topic.get(topic)
        if prior is not None and _numbers(prior) != nums:
            out.append((topic, prior, text))
        else:
            by_topic.setdefault(topic, text)
    return out


def consolidate(quote, *, actor, commit=False):
    """Approved customer-facing RFQ conditions become quote terms.

    (added, merged, conflicts). Internal notes never travel: that
    is the whole reason `rfq_conditions.visibility` is recorded at
    the moment somebody writes the line rather than inferred later.
    """
    from app import db
    from app.models.quote_terms import QuoteSpecialCondition
    from app.models.rfq import RfqCondition
    from app.services import audit

    if not quote.rfq_id:
        return [], 0, []

    rows = (RfqCondition.query
            .filter(RfqCondition.rfq_id == quote.rfq_id,
                    RfqCondition.visibility == RfqCondition.CUSTOMER,
                    RfqCondition.approved.is_(True))
            .order_by(RfqCondition.step_no, RfqCondition.id).all())

    existing = {_norm(c.text): c for c in
                QuoteSpecialCondition.query.filter_by(
                    quote_id=quote.id).all()}
    seq = max([c.seq or 0 for c in existing.values()] or [0])

    added, merged = [], 0
    for cond in rows:
        key = _norm(cond.text)
        if not key:
            continue
        if key in existing:
            merged += 1
            continue
        seq += 1
        row = QuoteSpecialCondition(quote_id=quote.id, seq=seq,
                                    text=cond.text, created_by=actor)
        db.session.add(row)
        existing[key] = row
        added.append(cond.text)

    found = conflicts([c.text for c in existing.values()])
    if added:
        audit.record('quote.conditions_consolidated', 'quote',
                     quote.id,
                     new={'added': len(added), 'merged': merged,
                          'conflicts': len(found)},
                     actor=actor,
                     reason='customer-facing conditions from the RFQ '
                            'steps; internal notes never travel')
    if commit:
        db.session.commit()
    return added, merged, found


def approval_block(quote):
    """Why approval must not proceed, or ''."""
    from app.models.quote_terms import QuoteSpecialCondition

    texts = [c.text for c in QuoteSpecialCondition.query.filter_by(
        quote_id=quote.id).all()]
    found = conflicts(texts)
    if not found:
        return ''
    topic, a, b = found[0]
    return (f'Conflict to resolve before approval — this quotation '
            f'says two things about {topic}: "{a[:70]}" and '
            f'"{b[:70]}".')
