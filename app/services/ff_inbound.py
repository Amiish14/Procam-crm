"""
A vendor replies; the CRM files it and reads a rate out of it.

FF-COMMS-01 R3. Two jobs, deliberately separate:

`link()` decides which rate request a reply belongs to. It reads the
T-series reference from the subject and the sender's address, and it
answers with a confidence it can defend. A reply filed against the
wrong job is worse than one left in the tray, so an ambiguous match
returns nothing and says why.

`extract()` reads an amount, a currency, a transit time and a
validity out of the body, and returns them as **suggestions**. It
never writes them. Vendor mail is prose written by people in a
hurry — "USD 1,450 all-in, 28-30 days, valid till month end" — and a
parser confident enough to save that unattended will eventually book
a job at the wrong price. A person confirms; the CRM does the
typing.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from app.services import crn as crn_svc

#: Currencies worth recognising in a freight quote.
CURRENCIES = ('INR', 'USD', 'EUR', 'GBP', 'AED', 'SGD', 'CNY', 'JPY')
_CUR_ALT = '|'.join(CURRENCIES)
_SYMBOL = {'₹': 'INR', '$': 'USD', '€': 'EUR', '£': 'GBP'}

#: `USD 1,450.00` / `1,450 USD` / `$1450`
_AMOUNT = re.compile(
    rf'(?:(?P<cur1>{_CUR_ALT})\s*|(?P<sym>[₹$€£])\s*)'
    rf'(?P<n1>\d[\d,]*(?:\.\d{{1,2}})?)'
    rf'|(?P<n2>\d[\d,]*(?:\.\d{{1,2}})?)\s*(?P<cur2>{_CUR_ALT})',
    re.IGNORECASE)

#: "28 days", "transit 21-24 days", "T/T 30 days"
_TRANSIT = re.compile(
    r'(?:transit|t/?t|sailing|lead\s*time)?\D{0,12}?'
    r'(?P<lo>\d{1,3})\s*(?:-|–|to)?\s*(?P<hi>\d{1,3})?\s*'
    r'(?:days?|dys?)\b', re.IGNORECASE)

_VALID = re.compile(
    r'valid(?:ity)?\s*(?:till|until|up\s*to|upto|through)?\s*:?\s*'
    r'(?P<d>\d{1,2})[/-](?P<m>\d{1,2})[/-](?P<y>\d{2,4})',
    re.IGNORECASE)


class LinkResult:
    """Why a reply was or was not filed, in a form a screen can show."""

    def __init__(self, request=None, *, confidence='none', reason='',
                 candidates=None):
        self.request = request
        self.confidence = confidence          # exact | likely | none
        self.reason = reason
        self.candidates = candidates or []

    @property
    def matched(self):
        return self.request is not None

    def to_dict(self):
        return {
            'matched': self.matched,
            'confidence': self.confidence,
            'reason': self.reason,
            'rate_request_id': (self.request.id if self.request
                                else None),
            'candidates': [r.id for r in self.candidates],
        }


def link(subject, from_addr, *, body=''):
    """Which rate request a vendor reply answers."""
    from app.models.ff import RateRequest
    from app.models.rfq import RFQ

    refs = crn_svc.find_in(subject) or crn_svc.find_in(body)
    vendor_refs = [r for r in refs
                   if crn_svc.parse(r)['kind'] == crn_svc.VENDOR]
    if not vendor_refs:
        return LinkResult(
            reason='No vendor reference in the subject. A reply the '
                   'CRM cannot place is left for a person rather than '
                   'guessed at.')
    if len(vendor_refs) > 1:
        return LinkResult(
            reason=f'The subject quotes {len(vendor_refs)} references '
                   f'({", ".join(vendor_refs)}); which job this answers '
                   f'is not something to guess.')

    rfq = RFQ.query.filter_by(vendor_crn=vendor_refs[0]).first()
    if rfq is None:
        return LinkResult(
            reason=f'{vendor_refs[0]} is not a reference this CRM '
                   f'issued.')

    rows = RateRequest.query.filter_by(rfq_id=rfq.id).all()
    if not rows:
        return LinkResult(
            reason=f'{vendor_refs[0]} has no rate requests against it.')

    addr = (from_addr or '').strip().lower()
    domain = addr.split('@', 1)[1] if '@' in addr else ''
    exact = [r for r in rows
             if (r.vendor_email or '').strip().lower() == addr and addr]
    if len(exact) == 1:
        return LinkResult(exact[0], confidence='exact',
                          reason='Reference and sender both match.')
    if len(exact) > 1:
        return LinkResult(
            candidates=exact,
            reason=f'{len(exact)} requests share that sender address on '
                   f'one job; a person should say which.')

    by_domain = [r for r in rows
                 if domain and (r.vendor_email or '').strip().lower()
                 .endswith('@' + domain)]
    if len(by_domain) == 1:
        return LinkResult(
            by_domain[0], confidence='likely',
            reason=f'Reference matches and the sender is at '
                   f'{domain}, though not the address we wrote to.')
    if len(rows) == 1:
        return LinkResult(
            rows[0], confidence='likely',
            reason='Reference matches and only one vendor was asked.')
    return LinkResult(
        candidates=by_domain or rows,
        reason=f'{vendor_refs[0]} matches, but the sender {addr or "?"} '
               f'is not one of the {len(rows)} we wrote to.')


def _money(text):
    for m in _AMOUNT.finditer(text or ''):
        cur = (m.group('cur1') or m.group('cur2') or '').upper()
        if not cur and m.group('sym'):
            cur = _SYMBOL.get(m.group('sym'), '')
        raw = m.group('n1') or m.group('n2') or ''
        try:
            value = float(raw.replace(',', ''))
        except ValueError:
            continue
        # A freight rate is not three rupees. Small numbers in a mail
        # are almost always container counts or days.
        if value < 100:
            continue
        return value, (cur or 'INR'), m.group(0).strip()
    return None, None, None


def extract(body, *, today=None):
    """Suggested amount, currency, transit and validity — never saved.

    Every value comes back with the text it was read from, so the
    person confirming can see what the parser looked at rather than
    taking a number on trust.
    """
    today = today or date.today()
    text = re.sub(r'<[^>]+>', ' ', body or '')
    text = re.sub(r'\s+', ' ', text)

    amount, currency, amount_src = _money(text)

    transit, transit_src = None, None
    m = _TRANSIT.search(text)
    if m:
        lo = int(m.group('lo'))
        hi = int(m.group('hi')) if m.group('hi') else None
        # A range is quoted as its longer end: promising the client
        # the optimistic number is how a schedule slips on day one.
        candidate = max(lo, hi) if hi else lo
        if 0 < candidate <= 180:
            transit, transit_src = candidate, m.group(0).strip()

    validity, validity_src = None, None
    v = _VALID.search(text)
    if v:
        try:
            year = int(v.group('y'))
            year += 2000 if year < 100 else 0
            validity = date(year, int(v.group('m')), int(v.group('d')))
            validity_src = v.group(0).strip()
        except ValueError:
            validity = None

    notes = []
    if amount is None:
        notes.append('No amount recognised — type it in.')
    if currency and currency != 'INR':
        notes.append(f'Quoted in {currency}; it will be converted at '
                     f'the rate on the day you confirm.')
    if transit is None:
        notes.append('No transit time found.')
    if validity and validity < today:
        notes.append(f'The validity read as {validity}, which is in '
                     f'the past — check it against the mail.')

    return {
        'amount': amount, 'amount_text': amount_src,
        'currency': currency, 'transit_days': transit,
        'transit_text': transit_src,
        'validity_until': validity, 'validity_text': validity_src,
        'notes': notes,
        'confirmed': False,
    }
