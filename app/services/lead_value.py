"""
The commercial numbers on a lead: what the deal is worth, and what was
quoted, when, until when, and at what cost.

Amounts are kept in the currency they were entered in, alongside the INR
value and the exchange rate used, so a later rate change does not move a
number someone already reported. Every list and report reads the INR
columns: ``estimated_value_inr`` (opportunity value) and
``quoted_amount_inr`` (quote value).

The quote is kept on the lead, latest version in columns. Re-quoting does
not overwrite: the version it replaces is appended to ``quote_revisions``
and ``quote_revision`` goes up by one.

``cost_million`` is the older value field, in millions, that the ₹M
columns used to read. It is still written, as a mirror of the opportunity
value, so imports and any older reader keep working.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

CURRENCIES = ('INR', 'USD', 'EUR')
VALUE_BASES = ('estimated', 'client_budget', 'firm')
VALUE_BASIS_LABELS = {'estimated': 'Estimated',
                      'client_budget': 'Client budget', 'firm': 'Firm'}

#: Master Data list holding INR per unit of each foreign currency.
FX_LIST = 'fx_rate'

#: Stages at which the quote block is shown on the lead.
QUOTE_STAGES = ('RFQ Generated', 'Quoted', 'Under Negotiation', 'Won',
                'Lost')
#: Stages where a lead is waiting on a quote the customer holds.
OPEN_QUOTE_STAGES = ('Quoted', 'Under Negotiation')

MILLION = Decimal('1000000')
LAKH = Decimal('100000')
CRORE = Decimal('10000000')

class LeadValueError(ValueError):
    """Input the lead cannot take; the message is shown to the user."""


# ── parsing ──────────────────────────────────────────────────────────
def _amount(v, label):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        d = Decimal(str(v).replace(',', '').replace('₹', '').strip())
    except (InvalidOperation, ValueError):
        raise LeadValueError(f'{label} must be a number')
    if not d.is_finite():
        raise LeadValueError(f'{label} must be a number')
    if d < 0:
        raise LeadValueError(f'{label} cannot be negative')
    if d >= Decimal('1e13'):
        raise LeadValueError(f'{label} is too large')
    return d.quantize(Decimal('0.01'))


def _day(v, label):
    if v is None or v == '':
        return None
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], '%Y-%m-%d').date()
    except ValueError:
        raise LeadValueError(f'{label} must be a date (YYYY-MM-DD)')


# ── exchange rates ───────────────────────────────────────────────────
def fx_rate(currency):
    """INR per one unit of ``currency``, or None when no rate is set."""
    currency = (currency or 'INR').upper()
    if currency == 'INR':
        return Decimal('1')
    from app.models.master_data import MasterItem
    item = MasterItem.query.filter_by(list_key=FX_LIST, code=currency,
                                      is_active=True).first()
    try:
        rate = Decimal(str((item.meta or {}).get('inr_per_unit')))
    except (AttributeError, InvalidOperation, TypeError, ValueError):
        return None
    return rate if rate.is_finite() and rate > 0 else None


def rates():
    """Every currency with its current rate, for the editor and the
    Master Data screen."""
    from app.models.master_data import MasterItem
    out = []
    for code in CURRENCIES:
        if code == 'INR':
            out.append({'currency': 'INR', 'inr_per_unit': 1.0,
                        'as_of': None, 'set_by': None})
            continue
        item = MasterItem.query.filter_by(list_key=FX_LIST,
                                          code=code).first()
        meta = (item.meta or {}) if item else {}
        rate = fx_rate(code) if item and item.is_active else None
        out.append({'currency': code,
                    'inr_per_unit': float(rate) if rate else None,
                    'as_of': meta.get('as_of'), 'set_by': meta.get('set_by')})
    return out


def set_rate(currency, inr_per_unit, actor):
    """Set one exchange rate. Leads already saved keep the rate they
    were converted at."""
    from app import db
    from app.master_data import service as md
    from app.models.master_data import MasterItem
    from app.services import audit

    currency = (currency or '').upper()
    if currency not in CURRENCIES or currency == 'INR':
        raise LeadValueError(f'Rates can be set for '
                             f'{", ".join(c for c in CURRENCIES if c != "INR")}')
    rate = _amount(inr_per_unit, 'The rate')
    if rate is None or rate <= 0:
        raise LeadValueError('The rate must be more than zero')
    md.ensure_lists()
    item = MasterItem.query.filter_by(list_key=FX_LIST, code=currency).first()
    if item is None:
        item = md.add_item(FX_LIST, currency, label=currency,
                           description=f'INR per 1 {currency}', actor=actor)
    old = dict(item.meta or {})
    history = list(old.get('history') or [])
    if old.get('inr_per_unit') is not None:
        history.append({'inr_per_unit': old.get('inr_per_unit'),
                        'as_of': old.get('as_of'), 'set_by': old.get('set_by')})
    item.meta = {'inr_per_unit': float(rate),
                 'as_of': datetime.utcnow().strftime('%Y-%m-%d %H:%M'),
                 'set_by': actor, 'history': history[-20:]}
    item.is_active = True
    audit.record('config.fx_rate', 'master_item', item.id,
                 old={'currency': currency,
                      'inr_per_unit': old.get('inr_per_unit')},
                 new={'currency': currency, 'inr_per_unit': float(rate)},
                 actor=actor)
    db.session.commit()
    return item


def _to_inr(amount, currency, label):
    if amount is None:
        return None, None
    rate = fx_rate(currency)
    if rate is None:
        raise LeadValueError(
            f'No exchange rate is set for {currency}. An administrator sets '
            f'it in Master Data → Exchange Rates; until then enter {label} '
            f'in INR.')
    return (amount * rate).quantize(Decimal('0.01')), rate


# ── reading ──────────────────────────────────────────────────────────
def value_inr(lead):
    """The single figure lists and reports use: the quote once there is
    one, the opportunity value before that, the old ₹M field last."""
    for v in (lead.quoted_amount_inr, lead.estimated_value_inr):
        if v is not None and Decimal(str(v)) > 0:
            return Decimal(str(v))
    if lead.cost_million:
        return (Decimal(str(lead.cost_million)) * MILLION).quantize(
            Decimal('0.01'))
    return None


def value_source(lead):
    if lead.quoted_amount_inr is not None and lead.quoted_amount_inr > 0:
        return 'quote'
    if lead.estimated_value_inr is not None and lead.estimated_value_inr > 0:
        return 'opportunity'
    if lead.cost_million:
        return 'legacy'
    return None


def value_inr_sql():
    """value_inr() as a SQL expression, for sums and sorting."""
    from app import db, Lead
    return db.func.coalesce(
        db.func.nullif(Lead.quoted_amount_inr, 0),
        db.func.nullif(Lead.estimated_value_inr, 0),
        db.func.nullif(Lead.cost_million, 0) * 1000000)


def row_value_inr(row):
    """value_inr() for a column row that carries the three fields."""
    for field in ('quoted_amount_inr', 'estimated_value_inr'):
        v = getattr(row, field, None)
        if v is not None and float(v) > 0:
            return float(v)
    return float(getattr(row, 'cost_million', 0) or 0) * 1_000_000


def margin(lead):
    """(margin in the quote currency, margin %) or (None, None)."""
    q, c = lead.quote_value_num, lead.quote_cost_num
    if q is None or c is None or Decimal(str(q)) <= 0:
        return None, None
    m = Decimal(str(q)) - Decimal(str(c))
    return m, (m / Decimal(str(q)) * 100).quantize(Decimal('0.1'))


def ageing(lead, today=None):
    """Days since the quote went out, days of validity left (negative once
    it has lapsed), and whether that matters at this stage."""
    today = today or date.today()
    since = (today - lead.quote_date).days if lead.quote_date else None
    left = ((lead.quote_validity_date - today).days
            if lead.quote_validity_date else None)
    open_ = (lead.stage or '') in OPEN_QUOTE_STAGES
    return {'days_since_quote': since, 'validity_days_left': left,
            'expired': bool(open_ and left is not None and left < 0),
            'open': open_}


def quote_suggestion(lead):
    """A quote read out of one of our own quotation emails, offered to the
    PIC to confirm. Never counted until they do."""
    import json
    try:
        notes = json.loads(lead.opp_notes or '{}')
        last = notes.get('last_quote') if isinstance(notes, dict) else None
    except (TypeError, ValueError):
        return None
    if not isinstance(last, dict) or not last.get('amount'):
        return None
    return {k: last.get(k) for k in ('amount', 'currency', 'quote_no',
                                     'quote_date', 'validity', 'recorded_from')
            if last.get(k) is not None}


def _f(v):
    return float(v) if v is not None else None


def to_dict(lead, today=None):
    m, pct = margin(lead)
    v = value_inr(lead)
    return {
        'value_inr': _f(v),
        'value_source': value_source(lead),
        'currency': lead.value_currency or 'INR',
        'opportunity_value': _f(lead.opportunity_value_num),
        'opportunity_value_inr': _f(lead.estimated_value_inr),
        'opportunity_fx_rate': _f(lead.opportunity_fx_rate),
        'value_basis': lead.value_basis or '',
        'quote_no': lead.quote_no or '',
        'quote_value': _f(lead.quote_value_num),
        'quote_value_inr': _f(lead.quoted_amount_inr),
        'quote_fx_rate': _f(lead.quote_fx_rate),
        'quote_date': str(lead.quote_date) if lead.quote_date else '',
        'quote_validity_date': (str(lead.quote_validity_date)
                                if lead.quote_validity_date else ''),
        'quote_cost': _f(lead.quote_cost_num),
        'quote_margin': _f(m),
        'quote_margin_pct': _f(pct),
        'quote_revision': lead.quote_revision or 0,
        'quote_revisions': list(lead.quote_revisions or []),
        'quote_recorded_by': lead.quote_recorded_by or '',
        'quote_recorded_at': (str(lead.quote_recorded_at)[:16]
                              if lead.quote_recorded_at else ''),
        'quote_ageing': ageing(lead, today),
        'quote_suggestion': quote_suggestion(lead),
    }


# ── writing ──────────────────────────────────────────────────────────
#: Request keys this module owns.
KEYS = ('currency', 'opportunity_value', 'value_basis', 'quote_no',
        'quote_value', 'quote_date', 'quote_validity_date',
        'quote_validity_days', 'quote_cost')


def _snapshot(lead):
    return {
        'revision': lead.quote_revision or 0,
        'quote_no': lead.quote_no,
        'currency': lead.value_currency or 'INR',
        'quote_value': _f(lead.quote_value_num),
        'quote_value_inr': _f(lead.quoted_amount_inr),
        'fx_rate': _f(lead.quote_fx_rate),
        'quote_date': str(lead.quote_date) if lead.quote_date else None,
        'quote_validity_date': (str(lead.quote_validity_date)
                                if lead.quote_validity_date else None),
        'quote_cost': _f(lead.quote_cost_num),
        'recorded_by': lead.quote_recorded_by,
        'recorded_at': (str(lead.quote_recorded_at)[:16]
                        if lead.quote_recorded_at else None),
    }


def _has_quote(lead):
    return any(getattr(lead, f) not in (None, '') for f in
               ('quote_value_num', 'quoted_amount_inr', 'quote_date',
                'quote_no'))


def _current(lead):
    """The lead's amounts as entered. A lead valued before amounts were
    kept separately has only the INR columns; those are its entered
    amounts, so saving it unchanged is not a change."""
    inr = (lead.value_currency or 'INR') == 'INR'
    return {
        'opportunity_value_num': (lead.opportunity_value_num
                                  if lead.opportunity_value_num is not None
                                  or not inr else lead.estimated_value_inr),
        'quote_no': lead.quote_no,
        'quote_value_num': (lead.quote_value_num
                            if lead.quote_value_num is not None or not inr
                            else lead.quoted_amount_inr),
        'quote_date': lead.quote_date,
        'quote_validity_date': lead.quote_validity_date,
        'quote_cost_num': lead.quote_cost_num,
    }


def apply(lead, data, actor):
    """Apply whichever of KEYS are in ``data``. Validates everything
    before changing anything. Returns the names of what changed."""
    if not any(k in data for k in KEYS):
        return []

    currency = (data.get('currency') if 'currency' in data
                else lead.value_currency) or 'INR'
    currency = str(currency).upper().strip()
    if currency not in CURRENCIES:
        raise LeadValueError(f'Currency must be one of {", ".join(CURRENCIES)}')

    basis = lead.value_basis
    if 'value_basis' in data:
        basis = (data.get('value_basis') or '').strip() or None
        if basis and basis not in VALUE_BASES:
            raise LeadValueError('Value basis must be Estimated, Client '
                                 'budget or Firm')

    opp = (_amount(data.get('opportunity_value'), 'Opportunity value')
           if 'opportunity_value' in data else lead.opportunity_value_num)

    q = {
        'quote_no': ((data.get('quote_no') or '').strip()[:40] or None
                     if 'quote_no' in data else lead.quote_no),
        'quote_value_num': (_amount(data.get('quote_value'), 'Quote value')
                            if 'quote_value' in data else lead.quote_value_num),
        'quote_date': (_day(data.get('quote_date'), 'Quote date')
                       if 'quote_date' in data else lead.quote_date),
        'quote_cost_num': (_amount(data.get('quote_cost'), 'Estimated cost')
                           if 'quote_cost' in data else lead.quote_cost_num),
    }
    if 'quote_validity_days' in data and data.get('quote_validity_days') not in (None, ''):
        try:
            days = int(str(data['quote_validity_days']).strip())
        except ValueError:
            raise LeadValueError('Validity must be a number of days')
        if days < 0 or days > 3650:
            raise LeadValueError('Validity must be between 0 and 3650 days')
        if not q['quote_date']:
            raise LeadValueError('Give the quote date to count validity days from')
        q['quote_validity_date'] = q['quote_date'] + timedelta(days=days)
    elif 'quote_validity_date' in data:
        q['quote_validity_date'] = _day(data.get('quote_validity_date'),
                                        'Validity date')
    else:
        q['quote_validity_date'] = lead.quote_validity_date
    if (q['quote_validity_date'] and q['quote_date']
            and q['quote_validity_date'] < q['quote_date']):
        raise LeadValueError('Validity cannot end before the quote date')
    if q['quote_date'] and q['quote_date'] > date.today() + timedelta(days=1):
        raise LeadValueError('The quote date cannot be in the future')

    old_currency = lead.value_currency or 'INR'
    currency_changed = currency != old_currency
    current = _current(lead)

    changed = []
    # ── the quote. Filling in a blank is not a re-quote; replacing a
    #    value that was there is, and the version it replaces is kept.
    quote_changed = currency_changed and _has_quote(lead)
    replaced = quote_changed
    for field, new in q.items():
        old = current[field]
        if old != new:
            quote_changed = True
            if old not in (None, ''):
                replaced = True
    if quote_changed:
        q_inr, q_rate = (_to_inr(q['quote_value_num'], currency,
                                 'the quote value')
                         if q['quote_value_num'] is not None else (None, None))
        if replaced and _has_quote(lead):
            revisions = list(lead.quote_revisions or [])
            revisions.append(_snapshot(lead))
            lead.quote_revisions = revisions
            lead.quote_revision = (lead.quote_revision or 0) + 1
        for field, new in q.items():
            setattr(lead, field, new)
        lead.quoted_amount_inr = q_inr
        lead.quote_fx_rate = q_rate if q_rate != 1 else None
        lead.quote_recorded_by = actor
        lead.quote_recorded_at = datetime.utcnow()
        changed.append('quote')

    # ── the opportunity value, converted only when it or the currency
    #    changes: a later rate must not move a saved figure.
    if opp != current['opportunity_value_num'] or currency_changed:
        opp_inr, opp_rate = (_to_inr(opp, currency, 'the opportunity value')
                             if opp is not None else (None, None))
        lead.opportunity_value_num = opp
        lead.estimated_value_inr = opp_inr
        lead.opportunity_fx_rate = opp_rate if opp_rate != 1 else None
        lead.cost_million = float(opp_inr / MILLION) if opp_inr else 0
        changed.append('opportunity_value')
    if basis != lead.value_basis:
        lead.value_basis = basis
        changed.append('value_basis')
    if currency_changed:
        lead.value_currency = currency
        changed.append('currency')
    return changed


def apply_legacy(lead, data, actor):
    """The fields older clients send: ``cost`` (₹ millions),
    ``estimated_value`` and ``quoted_amount`` (both ₹). They are INR by
    definition, so a lead kept in another currency refuses them rather
    than mixing units."""
    legacy = {}
    if 'estimated_value' in data:
        legacy['opportunity_value'] = data.get('estimated_value')
    elif 'cost' in data:
        try:
            m = Decimal(str(data.get('cost') or 0))
        except (InvalidOperation, ValueError):
            raise LeadValueError('Value must be a number')
        legacy['opportunity_value'] = (m * MILLION) if m else None
    if 'quoted_amount' in data:
        legacy['quote_value'] = data.get('quoted_amount')
    if not legacy:
        return []
    if (lead.value_currency or 'INR') != 'INR':
        raise LeadValueError(f'This lead is kept in {lead.value_currency}; '
                             f'send opportunity_value / quote_value instead')
    return apply(lead, legacy, actor)


def warnings_for_stage(lead, stage):
    """What a lead at ``stage`` is missing, for the soft prompts."""
    out = []
    if stage in OPEN_QUOTE_STAGES:
        if lead.quoted_amount_inr is None:
            out.append('quote_value')
        if lead.quote_date is None:
            out.append('quote_date')
    if stage == 'Won' and value_inr(lead) is None:
        out.append('value')
    return out


def format_inr(v):
    """₹ 12.5 L below a crore, ₹ 3.50 Cr from a crore up."""
    if v is None:
        return '—'
    v = Decimal(str(v))
    if v >= CRORE:
        return f'₹ {(v / CRORE):.2f} Cr'
    return f'₹ {(v / LAKH):.1f} L'
