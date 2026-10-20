"""
Ask several carriers, capture what comes back, compare, choose,
and turn the choice into a priced quote line.

FF-COMMS-01, R1. Everything here is behind `FEATURE_FF_CRM`; the
callers check the flag, this module does the work.

The shape of the thing
    A `RateSourcingLine` is a leg that needs a price. You ask three
    or four agents (a `RateRequest` each), some reply, you compare
    what arrived, you choose one and say why if it was not the
    cheapest, and the chosen rate becomes the *cost* of a sell line
    whose price you then set. Margin is the difference, and it is
    the number this whole release exists to make visible.

What it refuses to do
    It will not pick for you. `compare()` ranks and annotates; the
    choosing is `choose()` and takes a person. A cheapest-wins rule
    would be wrong about half the time — transit, validity and who
    actually performs are not in the number — and a system that
    chooses silently teaches people to stop reading.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app import db
from app.models.ff import (CHA_MODES, CHA_OWN, CHA_PARTNER, CANCELLED,
                           DECLINED, NO_RESPONSE, OWN_CHA_PORTS,
                           RateRequest, REPLIED, REQUESTED)


class RateError(ValueError):
    """Something the caller asked for cannot be stored as given."""


# ── the CHA rule ─────────────────────────────────────────────────────
def cha_mode_for(port):
    """Own at Chennai and Mumbai, partner everywhere else.

    Derived rather than typed, so the two ports live in one place.
    """
    name = (port or '').strip().lower()
    for own in OWN_CHA_PORTS:
        if own.lower() in name:
            return CHA_OWN
    return CHA_PARTNER


def check_cha(port, mode):
    """Refuse a customs line that claims an own-CHA port it is not.

    A quote built on "own CHA" at a port where we hold no licence
    prices work we cannot do.
    """
    if mode not in CHA_MODES:
        raise RateError(f'CHA mode must be one of {", ".join(CHA_MODES)}.')
    expected = cha_mode_for(port)
    if mode == CHA_OWN and expected != CHA_OWN:
        raise RateError(
            f'Own CHA is only {" and ".join(OWN_CHA_PORTS)}; '
            f'{port or "that port"} is a partner.')
    return mode


# ── asking ───────────────────────────────────────────────────────────
def request_rates(line, vendors, *, actor, due_by=None, note=None):
    """Raise one request per vendor for one leg.

    A vendor already asked and still open is not asked twice — the
    second ask would arrive as a duplicate in their inbox and would
    double-count in the comparison.
    """
    if not vendors:
        raise RateError('Name at least one carrier or agent to ask.')
    existing = {
        (r.vendor_name or '').strip().lower()
        for r in RateRequest.query.filter_by(line_id=line.id)
        .filter(RateRequest.status.in_((REQUESTED, REPLIED))).all()}

    made, skipped = [], []
    for v in vendors:
        name = (v.get('name') or '').strip() if isinstance(v, dict) else str(v).strip()
        if not name:
            continue
        if name.lower() in existing:
            skipped.append(name)
            continue
        row = RateRequest(
            rfq_id=line.rfq_id, line_id=line.id, vendor_name=name[:240],
            vendor_company_id=(v.get('company_id')
                               if isinstance(v, dict) else None),
            vendor_email=((v.get('email') or '').strip()[:320]
                          if isinstance(v, dict) else None),
            requested_by=actor, due_by=due_by, request_note=note,
            status=REQUESTED)
        db.session.add(row)
        made.append(row)
        existing.add(name.lower())
    db.session.flush()
    return made, skipped


# ── capturing ────────────────────────────────────────────────────────
def record_reply(req, *, amount, currency='INR', validity_until=None,
                 transit_days=None, remarks=None, actor=None,
                 attachment_path=None):
    """Store what a vendor came back with."""
    if req.status == CANCELLED:
        raise RateError('That request was cancelled; reinstate it first.')
    try:
        value = Decimal(str(amount).replace(',', ''))
    except Exception:
        raise RateError(f'{amount!r} is not an amount.')
    if value <= 0:
        raise RateError('A rate of zero is not a rate. Mark the vendor '
                        'as Declined instead.')

    from app.services import lead_value
    cur = (currency or 'INR').upper()
    req.amount = value
    req.currency = cur
    # Converted now and kept, so a comparison re-read next month
    # shows the choice as it was made rather than as today's rate
    # would have made it.
    try:
        req.amount_inr = (value if cur == 'INR'
                          else value * Decimal(str(lead_value.fx_rate(cur))))
    except Exception:
        req.amount_inr = value if cur == 'INR' else None
    req.validity_until = validity_until
    req.transit_days = transit_days
    req.reply_remarks = remarks
    req.attachment_path = attachment_path
    req.replied_at = datetime.utcnow()
    req.status = REPLIED
    return req


def mark(req, status, *, actor=None):
    if status not in (DECLINED, NO_RESPONSE, CANCELLED, REQUESTED):
        raise RateError(f'{status!r} is not a status a person sets.')
    req.status = status
    return req


# ── comparing ────────────────────────────────────────────────────────
def compare(line, *, today=None):
    """Every reply for a leg, ranked, with what is wrong with each.

    Returns rows carrying the request, its rank by cost, and any
    `flags` — an expired validity or a missing transit time does not
    disqualify a quote, but choosing one without noticing is how a
    rate that lapsed last week ends up in a client's quotation.
    """
    today = today or date.today()
    rows = RateRequest.query.filter_by(line_id=line.id).all()
    replied = [r for r in rows if r.status == REPLIED
               and r.amount_inr is not None]
    replied.sort(key=lambda r: r.amount_inr)

    cheapest = replied[0].amount_inr if replied else None
    fastest = min((r.transit_days for r in replied
                   if r.transit_days is not None), default=None)

    out = []
    for i, r in enumerate(replied, start=1):
        flags = []
        if r.validity_until and r.validity_until < today:
            flags.append(f'rate expired on {r.validity_until}')
        elif r.validity_until and (r.validity_until - today).days <= 3:
            flags.append(f'expires {r.validity_until}')
        if r.transit_days is None:
            flags.append('no transit time quoted')
        if r.currency and r.currency != 'INR':
            flags.append(f'quoted in {r.currency}, converted at the rate '
                         f'on {str(r.replied_at)[:10]}')
        out.append({
            'request': r,
            'rank': i,
            'is_cheapest': bool(cheapest is not None
                                and r.amount_inr == cheapest),
            'is_fastest': bool(fastest is not None
                               and r.transit_days == fastest),
            'over_cheapest_inr': (float(r.amount_inr - cheapest)
                                  if cheapest is not None else 0.0),
            'flags': flags,
        })

    waiting = [r for r in rows if r.status == REQUESTED]
    return {
        'line_id': line.id,
        'ranked': out,
        'replied': len(replied),
        'waiting': len(waiting),
        'no_reply': [r for r in rows
                     if r.status in (DECLINED, NO_RESPONSE)],
        'cheapest_inr': float(cheapest) if cheapest is not None else None,
    }


# ── choosing ─────────────────────────────────────────────────────────
def choose(req, *, actor, reason=None, today=None):
    """Pick one vendor for a leg. One per leg; choosing again moves it.

    A choice that is not the cheapest must say why. Not to police
    anybody — because that sentence is the answer to the question
    somebody asks in March, and nobody remembers by then.
    """
    today = today or date.today()
    if req.status != REPLIED:
        raise RateError('Only a vendor who has replied can be chosen.')
    if req.validity_until and req.validity_until < today:
        raise RateError(
            f'That rate expired on {req.validity_until}. Ask for a '
            f'fresh one rather than quoting a price the vendor has '
            f'withdrawn.')

    from app.models.ff import RateRequest as _RR
    siblings = _RR.query.filter(_RR.line_id == req.line_id,
                                _RR.status == REPLIED,
                                _RR.amount_inr.isnot(None)).all()
    cheapest = min((s.amount_inr for s in siblings), default=None)
    if (cheapest is not None and req.amount_inr is not None
            and req.amount_inr > cheapest and not (reason or '').strip()):
        raise RateError(
            'This is not the cheapest reply. Say why in one line — it '
            'is the answer to the question somebody asks three months '
            'from now.')

    for s in siblings:
        if s.id != req.id and s.chosen:
            s.chosen = False
    req.chosen = True
    req.chosen_at = datetime.utcnow()
    req.chosen_by = actor
    req.chosen_reason = (reason or '').strip()[:300] or None
    return req


def chosen_for(line):
    return RateRequest.query.filter_by(line_id=line.id,
                                       chosen=True).first()


# ── building the quote ───────────────────────────────────────────────
def quote_line_from(line, *, quote, sell_amount=None, markup_pct=None,
                    line_no=None):
    """A sell line priced from the rate that was chosen for a leg.

    `sell_amount` sets the price outright; `markup_pct` derives it
    from the cost. One or the other — a line that takes both would
    silently prefer one and the quote would disagree with the
    spreadsheet somebody checked it against.
    """
    from app.models.quote import QuoteLine
    from app.services import ff_charges

    if sell_amount is not None and markup_pct is not None:
        raise RateError('Give a price or a markup, not both.')

    picked = chosen_for(line)
    if picked is None:
        raise RateError(
            f'No rate has been chosen for "{line.service}". Compare the '
            f'replies and choose one before pricing it.')

    group = (line.charge_group or 'other')
    if not ff_charges.is_group(group):
        raise RateError(f'{group!r} is not a charge group.')

    cost = picked.amount_inr or Decimal('0')
    if sell_amount is not None:
        sell = Decimal(str(sell_amount).replace(',', ''))
    elif markup_pct is not None:
        sell = (cost * (Decimal('100') + Decimal(str(markup_pct)))
                / Decimal('100')).quantize(Decimal('0.01'))
    else:
        sell = cost        # at cost, and the margin column will say so

    ql = QuoteLine(
        quote_id=quote.id, line_no=line_no or line.line_no,
        service=line.service,
        description=(line.scope or '')[:2000] or line.service,
        quantity=Decimal('1'), unit='lot', unit_rate=sell,
        charge_group=group,
        cost_amount=picked.amount, cost_currency=picked.currency,
        cost_inr=cost, source_rate_request_id=picked.id,
        remarks=(f'{picked.vendor_name}'
                 + (f' · {picked.transit_days}d transit'
                    if picked.transit_days else '')))
    ql.recompute_total()
    db.session.add(ql)
    return ql


def margin_summary(quote):
    """Sell, cost and margin for a quote, and the lines with none.

    A line with no cost is not margin of 100%: it is a line nobody
    has costed, and reporting it as profit is how a quote goes out
    below water.
    """
    from app.models.quote import QuoteLine
    lines = QuoteLine.query.filter_by(quote_id=quote.id).all()
    sell = sum((Decimal(str(l.line_total or 0)) for l in lines),
               Decimal('0'))
    costed = [l for l in lines if l.cost_inr is not None]
    uncosted = [l for l in lines if l.cost_inr is None]
    cost = sum((Decimal(str(l.cost_inr)) for l in costed), Decimal('0'))
    margin = sell - cost
    return {
        'sell_inr': float(sell),
        'cost_inr': float(cost),
        'margin_inr': float(margin),
        'margin_pct': (float(margin) * 100.0 / float(sell)
                       if sell else None),
        'lines': len(lines),
        'uncosted_lines': len(uncosted),
        'reliable': not uncosted,
        'note': ('' if not uncosted else
                 f'{len(uncosted)} line(s) carry no cost, so the margin '
                 f'above is overstated by whatever they cost.'),
    }
