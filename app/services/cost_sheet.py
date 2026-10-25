"""
What a job costs us, beside what we are charging for it.

CRM-QUOTE-REV-02 §5.3, behind `FEATURE_QUOTE_REV_V2`.

Saving the cost sheet closes step 6 and creates the quote draft;
approval closes step 7. Both are events, which is why neither has
a "mark this done" button.

Where the cost comes from
    Freight forwarding has rate tables, so the cost is prefilled
    from the lowest vendor reply actually received. Every other
    vertical types it. That asymmetry is real and worth leaving
    visible: a prefilled zero in a vertical with no rate tables
    would look like a priced job at no cost, and somebody would
    approve it.

The margin floor (D10)
    Shown live against the floor for the vertical. Below the
    floor, or above ₹25 lakh, the quote needs a director rather
    than the vertical head. The rule is stated here and enforced
    at approval, not only drawn on the screen.
"""
from __future__ import annotations

#: D10, 21 Oct. Fraction of the sell price.
MARGIN_FLOORS = {
    'PTM own hydraulic': 0.38,
    'Heavy Transport': 0.38,
    'General Transport': 0.14,
    'Mechanical trailer': 0.12,
    'Freight Forwarding': 0.10,
    'Project Freight': 0.10,
    'Warehousing': 0.18,
    'Installation': 0.25,
}
DEFAULT_FLOOR = 0.10

#: Above this a director approves, whatever the margin (D10).
DIRECTOR_ABOVE = 2_500_000.0

#: Verticals whose cost can be read from rate tables today.
PREFILL_VERTICALS = ('Freight Forwarding', 'Project Freight')


def floor_for(vertical):
    from app.services import admin_settings
    try:
        table = admin_settings.get('margin_floors') or {}
        if isinstance(table, dict) and vertical in table:
            return float(table[vertical])
    except Exception:                                     # noqa: BLE001
        pass
    return float(MARGIN_FLOORS.get(vertical, DEFAULT_FLOOR))


def prefill_cost(quote):
    """(cost, source) from rate tables, or (None, why not).

    The lowest vendor reply we have actually received — not the
    lowest we asked for. A quote priced off a rate nobody has
    confirmed is a guess wearing a number.
    """
    vertical = _vertical_of(quote)
    if vertical not in PREFILL_VERTICALS:
        return None, (f'{vertical or "this vertical"} has no rate '
                      f'tables — type the cost')
    try:
        from app.services import ff_rates
        summary = ff_rates.margin_summary(quote)
        cost = float(summary.get('cost') or 0)
        if cost <= 0:
            return None, 'no vendor replies received yet'
        return cost, 'the lowest vendor reply received'
    except Exception:                                     # noqa: BLE001
        return None, 'rate tables could not be read — type the cost'


def _vertical_of(record):
    from app.services.vertical_scope import vertical_name
    return vertical_name(record)


def margin(cost, sell):
    """Fraction of the sell price, or None when there is no sell.

    None rather than zero: a quote with no price has no margin,
    and calling that 0% puts a real-looking number on a line
    nobody has priced.
    """
    try:
        cost, sell = float(cost or 0), float(sell or 0)
    except (TypeError, ValueError):
        return None
    if sell <= 0:
        return None
    return (sell - cost) / sell


def verdict(quote, cost, sell):
    """What the screen says, and what approval will enforce."""
    vertical = _vertical_of(quote)
    floor = floor_for(vertical)
    got = margin(cost, sell)
    try:
        amount = float(sell or 0)
    except (TypeError, ValueError):
        amount = 0.0

    if got is None:
        return {'margin': None, 'floor': floor, 'vertical': vertical,
                'needs_director': amount > DIRECTOR_ABOVE,
                'words': 'no quoted price yet'}
    needs = got < floor or amount > DIRECTOR_ABOVE
    if got < floor:
        words = (f'{got * 100:.1f}% is below the {floor * 100:.0f}% '
                 f'floor for {vertical or "this vertical"} — needs '
                 f'a director')
    elif amount > DIRECTOR_ABOVE:
        words = (f'{got * 100:.1f}%, and over '
                 f'₹{DIRECTOR_ABOVE / 100000:.0f} lakh — needs a '
                 f'director')
    else:
        words = f'{got * 100:.1f}% against a {floor * 100:.0f}% floor'
    return {'margin': got, 'floor': floor, 'vertical': vertical,
            'needs_director': needs, 'words': words}


def save(quote, *, cost, sell, currency, actor, remarks=''):
    """Record the cost sheet and close step 6. (ok, detail).

    §5.3's submit gate: an empty amount or currency is refused
    here rather than at sending, because a quote with no number
    on it travels a long way before anybody notices.
    """
    from app import db
    from app.services import audit, rfq_chain

    try:
        cost_v = float(cost)
        sell_v = float(sell)
    except (TypeError, ValueError):
        return False, 'cost and quoted price must both be numbers'
    if sell_v <= 0:
        return False, ('a quoted price is required — a quote with no '
                       'number on it travels a long way before '
                       'anybody notices')
    if not (currency or '').strip():
        return False, 'choose a currency'
    if cost_v < 0:
        return False, 'cost cannot be negative'

    quote.currency = currency.strip()[:6]
    quote.total_amount = sell_v
    from datetime import datetime
    was = quote.cost_amount
    quote.cost_amount = cost_v
    quote.cost_saved_at = datetime.utcnow()
    quote.cost_saved_by = actor

    checked = verdict(quote, cost_v, sell_v)
    audit.record('quote.cost_sheet_saved', 'quote', quote.id,
                 old={'cost': float(was) if was is not None
                      else None},
                 new={'cost': cost_v, 'sell': sell_v,
                      'currency': quote.currency,
                      'margin': checked['margin'],
                      'needs_director': checked['needs_director']},
                 actor=actor,
                 reason=remarks or checked['words'])

    closed = None
    rfq = _rfq_of(quote)
    if rfq is not None and rfq_chain.enabled():
        ok, detail = rfq_chain.close_step(
            rfq, 6, actor=actor, event='cost_sheet_saved',
            reason=remarks if isinstance(remarks, str) else '',
            evidence={'cost': cost_v, 'sell': sell_v,
                      'currency': quote.currency,
                      'margin': checked['margin'],
                      'quote_id': quote.id})
        closed = detail if ok else None
        if not ok and isinstance(detail, str) and 'reason' in detail:
            db.session.rollback()
            return False, detail
    db.session.flush()
    return True, {'margin': checked, 'step6': closed}


def on_approved(quote, *, actor):
    """Approval closes step 7. (ok, detail)."""
    from app.services import quote_rev, rfq_chain

    block = quote_rev.approval_block(quote)
    if block:
        return False, block
    rfq = _rfq_of(quote)
    if rfq is None or not rfq_chain.enabled():
        return False, 'no chain on this quote'
    return rfq_chain.close_step(
        rfq, 7, actor=actor, event='quote_approved',
        evidence={'quote_id': quote.id,
                  'display': quote_rev.display_number(quote),
                  'approved_by': actor})


def _rfq_of(quote):
    from app import db
    from app.models.rfq import RFQ
    if not getattr(quote, 'rfq_id', None):
        return None
    return db.session.get(RFQ, quote.rfq_id)
