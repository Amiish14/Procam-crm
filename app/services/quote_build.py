"""
Quote builder v2: one quote table, every vertical.

CRM 2.0 R5. The forwarding release built the pieces — charge groups
on `quote_lines`, cost beside sell, margin that knows when it is
unreliable. This does not rebuild any of that. It makes the same
machinery answer for a transport, installation or warehousing quote,
and it makes the lead stop carrying its own private copy of the
numbers.

The mirror
    `leads` has six quote columns — quote_no, quote_value_num,
    quote_date, quote_validity_date, quoted_amount_inr,
    quote_revision — and the `quotes` table has none in use, because
    every quote this CRM has ever recorded was typed onto the lead.
    Two places to look is how they disagree.

    From here the quote is the record and the lead columns are a
    mirror of the latest one, rewritten whenever the quote changes.
    They are not deleted: every screen, report and export in the CRM
    reads them, and a migration that broke all of those at once to
    tidy a model would be a bad trade.

What a group means outside forwarding
    `ff_charges.groups_for(vertical)` decides which groups a quote
    may use, and `billable_groups` stops consulting the Incoterm in
    the verticals that have none. A lorry from Pune to Nashik has no
    Incoterm, and a field nobody can fill is a field people put
    anything in.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app import db
from app.services import ff_charges


class QuoteBuildError(ValueError):
    """Something the caller asked for cannot be stored as given."""


def vertical_of(quote):
    from app.services import quote_terms
    return quote_terms.vertical_of(quote)


def groups_for_quote(quote):
    return ff_charges.groups_for(vertical_of(quote))


def add_line(quote, *, service, charge_group, amount, description='',
             quantity=1, unit=None, unit_rate=None, cost_amount=None,
             cost_currency='INR', source_rate_request_id=None,
             actor=None):
    """One sell line, in a group this vertical actually uses."""
    from app.models.quote import QuoteLine
    from app.services import audit

    vertical = vertical_of(quote)
    group = (charge_group or 'other').strip()
    if not ff_charges.is_group_for(group, vertical):
        raise QuoteBuildError(
            f'{group!r} is not a charge group a {vertical} quote '
            f'uses. Available: '
            f'{", ".join(ff_charges.groups_for(vertical))}.')
    try:
        amount = Decimal(str(amount))
    except Exception:                                     # noqa: BLE001
        raise QuoteBuildError(f'{amount!r} is not an amount.')

    rows = (QuoteLine.query.filter_by(quote_id=quote.id)
            .order_by(QuoteLine.line_no.desc()).first())
    line = QuoteLine(
        quote_id=quote.id, line_no=(rows.line_no + 1) if rows else 1,
        service=(service or '')[:80], description=description,
        quantity=quantity, unit=unit, unit_rate=unit_rate,
        line_total=amount, charge_group=group,
        cost_amount=cost_amount, cost_currency=cost_currency,
        cost_inr=cost_amount if cost_currency == 'INR' else None,
        source_rate_request_id=source_rate_request_id)
    db.session.add(line)
    db.session.flush()
    audit.record('quote.line_added', 'quote', str(quote.id),
                 new={'line_no': line.line_no, 'group': group,
                      'amount': float(amount)},
                 actor=actor, reason=f'{vertical} quote line')
    return line


def totals(quote):
    """Sell, cost and margin, and whether the margin can be trusted."""
    from app.services import ff_rates
    return ff_rates.margin_summary(quote)


def recalculate(quote, *, actor=None, mirror=True):
    """Re-add the lines, stamp the terms, and refresh the lead.

    Called after anything changes a line. Cheap enough to call twice
    and safe to call on a quote with no lines.
    """
    from app.models.quote import QuoteLine
    from app.services import quote_terms

    lines = (QuoteLine.query.filter_by(quote_id=quote.id)
             .order_by(QuoteLine.line_no.asc()).all())
    subtotal = sum((Decimal(str(l.line_total or 0)) for l in lines),
                   Decimal('0'))
    quote.subtotal = subtotal
    tax = Decimal(str(quote.tax_amount or 0))
    quote.total_amount = subtotal + tax

    quote_terms.attach_to(quote, actor=actor)
    if mirror:
        mirror_to_lead(quote, actor=actor)
    return quote


#: The lead columns that are a mirror of the quote. Named here so
#: that the one place writing them is obvious when somebody asks why
#: a lead's figure changed on its own.
MIRRORED = ('quote_no', 'quote_value_num', 'quote_date',
            'quote_validity_date', 'quoted_amount_inr',
            'quote_revision')


def latest_quote_for(lead_id):
    from app.models.quote import Quote

    return (Quote.query.filter_by(lead_id=lead_id)
            .order_by(Quote.id.desc()).first())


def mirror_to_lead(quote, *, actor=None):
    """Copy the quote's figures onto the lead it belongs to.

    Only for the latest quote on that lead: editing an old revision
    must not pull the lead's headline figure backwards.
    """
    from app import Lead
    from app.services import audit

    lead_id = getattr(quote, 'lead_id', None)
    if not lead_id:
        return None
    lead = db.session.get(Lead, lead_id)
    if lead is None:
        return None
    latest = latest_quote_for(lead_id)
    if latest is not None and latest.id != quote.id:
        return None

    before = {f: _plain(getattr(lead, f, None)) for f in MIRRORED}
    lead.quote_no = quote.quote_number
    lead.quote_value_num = quote.total_amount
    lead.quoted_amount_inr = (quote.total_amount
                              if (quote.currency or 'INR') == 'INR'
                              else lead.quoted_amount_inr)
    lead.quote_date = quote.quote_date
    lead.quote_validity_date = quote.validity_until
    after = {f: _plain(getattr(lead, f, None)) for f in MIRRORED}
    if before == after:
        return None

    # A value that arrived from a quote the owner built is confirmed
    # by definition: they typed the lines it adds up from.
    if lead.quoted_amount_inr:
        lead.value_source = 'quote'
        lead.value_confirmed = True

    audit.record('lead.quote_mirrored', 'lead', str(lead.id),
                 old=before, new=after, actor=actor,
                 reason=f'mirrored from quote '
                        f'{quote.quote_number or quote.id}; the quote '
                        f'is the record and these columns follow it')
    return lead


def _plain(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return str(value)
    return value


def billable_split(quote):
    """(billable, not billable) for this quote's vertical."""
    from app.models.quote import QuoteLine

    lines = (QuoteLine.query.filter_by(quote_id=quote.id)
             .order_by(QuoteLine.line_no.asc()).all())
    vertical = vertical_of(quote)
    if not ff_charges.uses_incoterms(vertical):
        return lines, []
    return ff_charges.split_lines(lines, quote.incoterm,
                                  quote.client_party, vertical)
