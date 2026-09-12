"""
What a quotation email actually says — §6.

Detecting that an email *is* a quote submission is not the same as
knowing what was quoted. This pulls out the reference, date, amount and
currency so the lead records the commercial fact rather than just a
stage change.

Everything is optional. A quote with no extractable amount is still a
quote, and inventing a number would be worse than leaving it blank.
"""
from __future__ import annotations

import re
from datetime import date, datetime

#: Procam quotation references, and the common client-side shapes.
_REF_PATTERNS = (
    r'\b(?:PCM|PRO|PGL|PCL)[/\-\s]?(?:QT|QTN|Q)?[/\-\s]?\d{2,6}[/\-\s]?\d{0,4}\b',
    r'\b(?:quotation|quote|offer)\s*(?:no\.?|number|ref\.?|#)\s*[:\-]?\s*'
    r'([A-Z0-9][A-Z0-9/\-]{3,24})\b',
    r'\b(?:QTN|QT)[/\-]\d{2,6}[/\-]?[0-9\-]*\b',
)

_CURRENCIES = {
    '₹': 'INR', 'rs': 'INR', 'rs.': 'INR', 'inr': 'INR', 'rupees': 'INR',
    '$': 'USD', 'usd': 'USD', 'us$': 'USD',
    '€': 'EUR', 'eur': 'EUR', '£': 'GBP', 'gbp': 'GBP',
    'aed': 'AED', 'sgd': 'SGD', 'jpy': 'JPY', 'cny': 'CNY',
}

#: An amount with a currency beside it. Deliberately requires the
#: currency: a bare number in a logistics email is far more likely to be
#: a weight, a container count or a PIN code than a price.
_AMOUNT_RE = re.compile(
    r'(?P<cur>₹|\$|€|£|\b(?:rs\.?|inr|usd|us\$|eur|gbp|aed|sgd|jpy|cny)\b)'
    r'\s*(?P<num>\d[\d,]*(?:\.\d{1,2})?)'
    r'\s*(?P<scale>lakhs?|lacs?|crores?|cr\b|mn\b|million|k\b)?',
    re.I)

_SCALES = {'lakh': 1e5, 'lakhs': 1e5, 'lac': 1e5, 'lacs': 1e5,
           'crore': 1e7, 'crores': 1e7, 'cr': 1e7,
           'mn': 1e6, 'million': 1e6, 'k': 1e3}

_DATE_PATTERNS = (
    (r'\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b', 'dmy'),
    (r'\b(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})\b', 'ymd'),
    (r'\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)'
     r'[a-z]*\s+(\d{4})\b', 'dmon'),
)
_MONTHS = {m: i for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun',
     'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], start=1)}


def extract(text, *, received=None, attachments=()):
    """Everything the quote says, as far as it can be read.

    Returns {'reference', 'quoted_on', 'amount', 'currency'} with None
    for anything not found.
    """
    text = text or ''
    return {
        'reference': _reference(text, attachments),
        'quoted_on': _quoted_on(text) or _as_date(received),
        **_amount(text),
    }


def _reference(text, attachments=()):
    for pattern in _REF_PATTERNS:
        m = re.search(pattern, text, re.I)
        if m:
            ref = (m.group(1) if m.groups() else m.group(0)).strip(' :-')
            if len(ref) >= 4:
                return ref.upper()[:60]
    # A filename often carries it when the body does not. Separators are
    # normalised first: there is no word boundary inside
    # "Procam_Quotation_5512", so \b would never reach the reference.
    for name in (attachments or []):
        flat = re.sub(r'[_\-.]+', ' ', str(name))
        m = re.search(r'\b(?:PCM|QTN?|QUOT\w*)\s*(\d{2,6}[\w\-]*)', flat, re.I)
        if m:
            return f'QUOTE-{m.group(1)}'.upper()[:60]
    return None


def _quoted_on(text):
    low = text.lower()
    # Only a date sitting near quote wording — an email is full of dates.
    window = None
    for cue in ('quotation dated', 'quote dated', 'offer dated',
                'quotation date', 'quote date', 'dated'):
        i = low.find(cue)
        if i >= 0:
            window = text[i:i + 60]
            break
    for source in (window, text[:400]):
        if not source:
            continue
        for pattern, order in _DATE_PATTERNS:
            m = re.search(pattern, source, re.I)
            if not m:
                continue
            try:
                if order == 'dmy':
                    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                elif order == 'ymd':
                    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                else:
                    d = int(m.group(1))
                    mo = _MONTHS[m.group(2).lower()[:3]]
                    y = int(m.group(3))
                return date(y, mo, d)
            except (ValueError, KeyError):
                continue
    return None


def _amount(text):
    best_value, best_currency = None, None
    for m in _AMOUNT_RE.finditer(text):
        raw = m.group('num').replace(',', '')
        try:
            value = float(raw)
        except ValueError:
            continue
        scale = (m.group('scale') or '').lower().strip()
        if scale:
            value *= _SCALES.get(scale, 1)
        # The largest figure quoted is the one that matters; line items
        # and part-charges sit below the total.
        if best_value is None or value > best_value:
            best_value = value
            best_currency = _CURRENCIES.get(
                m.group('cur').lower().strip(), None)
    if best_value is None:
        return {'amount': None, 'currency': None}
    return {'amount': round(best_value, 2), 'currency': best_currency}


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and len(value) >= 10:
        try:
            return datetime.strptime(value[:10], '%Y-%m-%d').date()
        except ValueError:
            return None
    return None
