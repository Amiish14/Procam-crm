"""
Quote margins: who may see them, and when a quote needs the director.

Decisions of 21 Oct 2026 (CRM questionnaire G9, H1, H3, H4).

**Who sees buy rates and margin (G9).** On Freight Forwarding / Project
Freight quotes, only the people named in the `margin_viewers_ff`
setting — four people by management decision. On every other vertical:
an unrestricted (whole-company) viewer, the quote's preparer, the lead
owner, and the head of the quote's vertical. Everyone else gets the
quote without cost, margin or margin %.

**Minimum margin (H1).** One minimum per service line, on sell, in the
`margin_floors` setting:

    PTM hydraulic, own vehicle      38%
    PTM hydraulic, market vehicle   14%
    Mechanical trailer              12%
    Freight forwarding (sea & air)  10%
    Warehousing                     18%
    Installation                    25%

A Heavy Transport line says which it is through `vehicle_source`
(PTM-Own, PTM-Market, Mechanical); blank is read as Mechanical.

**Approval (H3, H4).** A vertical head may approve a quote at or above
every line's minimum and up to `head_approval_limit_inr`. Below a
minimum, or above the limit, only the people in `director_approvers`
may approve (the director, and whoever stands in when he is away).

The approval rule is behind FEATURE_MARGIN_GATE so it can be switched
on after the floors have been checked against live quotes. Visibility
is not behind a flag: it is a confidentiality rule.
"""
from __future__ import annotations

from decimal import Decimal

#: vehicle_source values a Heavy Transport line may carry.
VEHICLE_SOURCES = ('PTM-Own', 'PTM-Market', 'Mechanical')

_HT_KEYS = {'PTM-Own': 'PTM_OWN', 'PTM-Market': 'PTM_MARKET',
            'Mechanical': 'MECHANICAL'}
_VERTICAL_KEYS = {'Project Freight': 'FREIGHT_FORWARDING',
                  'Warehousing': 'WAREHOUSING',
                  'Installation': 'INSTALLATION'}

#: Keys stripped from a quote line for someone who may not see buy.
COST_KEYS = ('cost_amount', 'cost_currency', 'cost_inr', 'margin_inr',
             'margin_pct', 'source_rate_request_id')

#: Shown next to a line that has no cost, so nobody reads "no margin
#: problem" where the truth is "no cost entered".
NO_COST = 'no cost entered'


# ── which vertical / service line ────────────────────────────────────
def quote_vertical(quote):
    """The managed vertical (one of the four) of a quote."""
    from app.services import quote_terms, vertical_heads
    return vertical_heads.master_vertical(quote_terms.vertical_of(quote))


def line_key(line, vertical):
    if vertical == 'Heavy Transport':
        return _HT_KEYS.get((line.vehicle_source or '').strip(), 'MECHANICAL')
    return _VERTICAL_KEYS.get(vertical)


def floor_for(key):
    from app.services import admin_settings
    floors = admin_settings.get('margin_floors') or {}
    value = floors.get(key) if key else None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ── visibility ───────────────────────────────────────────────────────
def can_see_buy(quote, emp_code=None, sc=None):
    from flask import session

    from app import Employee, Lead
    from app.access import scope as sc_mod
    from app.services import admin_settings

    emp_code = emp_code or session.get('emp_code')
    if not emp_code:
        return False
    vertical = quote_vertical(quote)
    if vertical == 'Project Freight':
        return emp_code in (admin_settings.get('margin_viewers_ff') or [])
    sc = sc or sc_mod.for_employee(emp_code)
    if sc.unrestricted:
        return True
    if emp_code in (quote.prepared_by_id, quote.created_by_id):
        return True
    if quote.lead_id:
        lead = Lead.query.get(quote.lead_id)
        if lead is not None and emp_code == lead.assigned_to:
            return True
    emp = Employee.query.filter_by(emp_code=emp_code).first()
    if emp is not None and emp.is_vertical_head:
        from app.services import vertical_heads
        return vertical_heads.master_vertical(emp.vertical) == vertical
    return False


def redact_line(d):
    for key in COST_KEYS:
        if key in d:
            d[key] = None
    d['cost_hidden'] = True
    return d


def quote_dict(quote, *, deep=False, emp_code=None):
    """`quote.to_dict`, with buy and margin removed for those who may
    not see them."""
    d = quote.to_dict(deep=deep)
    if can_see_buy(quote, emp_code):
        d['cost_hidden'] = False
        return d
    d['cost_hidden'] = True
    for ln in d.get('lines') or []:
        redact_line(ln)
    return d


# ── approval ─────────────────────────────────────────────────────────
def check(quote):
    """Where a quote stands against the rules.

    {'below_floor': [..], 'no_cost': [..], 'over_limit': bool,
     'needs_director': bool, 'limit_inr': n}
    """
    from app.models.quote import QuoteLine
    from app.services import admin_settings

    vertical = quote_vertical(quote)
    below, no_cost = [], []
    for ln in QuoteLine.query.filter_by(quote_id=quote.id).all():
        key = line_key(ln, vertical)
        floor = floor_for(key)
        if floor is None or not ln.line_total:
            continue
        pct = ln.margin_pct
        if pct is None:
            no_cost.append({'line_no': ln.line_no, 'service': ln.service or '',
                            'note': NO_COST})
            continue
        if pct + 1e-9 < floor:
            below.append({'line_no': ln.line_no, 'service': ln.service or '',
                          'margin_pct': round(pct, 2), 'floor_pct': floor,
                          'service_line': key})
    try:
        limit = Decimal(str(admin_settings.get('head_approval_limit_inr') or 0))
    except Exception:
        limit = Decimal('0')
    value = Decimal(str(quote.subtotal or 0))
    if (quote.currency or 'INR').upper() != 'INR':
        # A foreign-currency total is not compared with a rupee limit
        # it cannot be converted against here; it goes to the director.
        over = True
    else:
        over = bool(limit) and value > limit
    return {'vertical': vertical, 'below_floor': below, 'no_cost': no_cost,
            'over_limit': over, 'limit_inr': float(limit),
            'needs_director': bool(below) or over}


def is_director(emp_code):
    from app.services import admin_settings
    return bool(emp_code) and emp_code in (
        admin_settings.get('director_approvers') or [])


def may_approve(quote, emp_code):
    """(allowed, reason) under the decided approval rule.

    Only consulted when FEATURE_MARGIN_GATE is on.
    """
    from app import Employee
    from app.services import vertical_heads

    result = check(quote)
    if is_director(emp_code):
        return True, ''
    if result['needs_director']:
        parts = []
        if result['below_floor']:
            parts.append('margin below the minimum on line(s) '
                         + ', '.join(str(b['line_no'])
                                     for b in result['below_floor']))
        if result['over_limit']:
            parts.append('value above the head approval limit')
        return False, ('This quote needs the director\'s approval: '
                       + '; '.join(parts) + '.')
    emp = Employee.query.filter_by(emp_code=emp_code).first()
    if (emp is not None and emp.is_vertical_head
            and vertical_heads.master_vertical(emp.vertical)
            == result['vertical']):
        return True, ''
    return False, 'Only the head of this vertical (or the director) may approve.'
