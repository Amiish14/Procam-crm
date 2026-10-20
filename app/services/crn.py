"""
The Client Reference Number: <BU>-<C|T><FY>-<NNNNNN>.

FF-COMMS-01 R2. `PWL-C26-000001` is the customer side of an enquiry;
`PWL-T26-000001` is the vendor side of the same work. One number
follows a job from the first email to the invoice, and it is what
makes a reply findable: a vendor quoting against `PWL-T26-000014`
can be filed without anybody reading the body.

Two series, and the reason is confidentiality
    The customer series (C) and the vendor series (T) are separate
    counters, not two faces of one number. If a vendor saw
    `PWL-C26-000001` they would hold the same reference the client
    holds, and two vendors comparing notes could line their quotes
    up against one job and against each other. The T-number is the
    only reference that ever leaves the building towards a vendor.

Gap-free, and what that costs
    The counter is incremented in the caller's transaction and the
    row that uses the number is written in the same one. Roll the
    transaction back and the increment goes with it, so no number is
    burnt — which is what "gap-free" has to mean in practice. It
    does mean two enquiries created at the same instant serialise on
    one row for the length of a transaction; at this volume that is
    nothing, and the alternative is holes in a sequence the business
    uses to audit itself.
"""
from __future__ import annotations

import re
from datetime import date

from app import db

#: Customer side and vendor side.
CUSTOMER, VENDOR = 'C', 'T'
KINDS = (CUSTOMER, VENDOR)

#: `PWL-C26-000001`. The BU is two to five letters; the business
#: writes PWL and PLPL and the register holds PLPL and PWLPL, so the
#: pattern accepts the range rather than a fixed list that would
#: refuse a unit nobody told the CRM about.
PATTERN = re.compile(r'^(?P<bu>[A-Z]{2,5})-(?P<kind>[CT])'
                     r'(?P<fy>\d{2})-(?P<serial>\d{6})$')

WIDTH = 6


class CrnError(ValueError):
    """A reference that cannot be formed or read as given."""


def financial_year(on=None):
    """India's financial year as two digits: April to March.

    20 October 2026 is FY 2026-27, which is `26`. Getting this wrong
    by a year silently restarts the sequence in the middle of a year
    or carries it an extra twelve months, and either way the numbers
    stop meaning what people think.
    """
    on = on or date.today()
    year = on.year if on.month >= 4 else on.year - 1
    return f'{year % 100:02d}'


def format_crn(bu, kind, fy, serial):
    bu = (bu or '').strip().upper()
    if not re.fullmatch(r'[A-Z]{2,5}', bu):
        raise CrnError(f'{bu!r} is not a business unit code.')
    if kind not in KINDS:
        raise CrnError(f'{kind!r} is not C (customer) or T (vendor).')
    if not re.fullmatch(r'\d{2}', str(fy)):
        raise CrnError(f'{fy!r} is not a two-digit financial year.')
    if not (0 < int(serial) < 10 ** WIDTH):
        raise CrnError(f'{serial} is outside the {WIDTH}-digit range.')
    return f'{bu}-{kind}{fy}-{int(serial):0{WIDTH}d}'


def parse(crn):
    """{'bu','kind','fy','serial'} or raise."""
    m = PATTERN.match((crn or '').strip().upper())
    if not m:
        raise CrnError(f'{crn!r} is not a CRN of the form '
                       f'<BU>-<C|T><FY>-<NNNNNN>.')
    d = m.groupdict()
    d['serial'] = int(d['serial'])
    return d


def find_in(text):
    """Every CRN mentioned in a string, in order, deduplicated.

    Used on reply subjects. Returns a list because a reply that
    quotes two references is a real thing and picking one silently
    is how mail gets filed against the wrong job.
    """
    seen, out = set(), []
    for m in re.finditer(r'\b[A-Z]{2,5}-[CT]\d{2}-\d{6}\b',
                         (text or '').upper()):
        crn = m.group(0)
        if crn not in seen:
            seen.add(crn)
            out.append(crn)
    return out


def allocate(bu, kind, *, fy=None, on=None):
    """The next number in a series. Caller commits.

    Deliberately does not commit: the number and the record that
    carries it must land together or not at all, which is the whole
    of the gap-free guarantee.
    """
    from app.models.ff import CrnSequence

    bu = (bu or '').strip().upper()
    if kind not in KINDS:
        raise CrnError(f'{kind!r} is not C or T.')
    fy = fy or financial_year(on)

    row = (CrnSequence.query
           .filter_by(bu=bu, kind=kind, fy=fy)
           .with_for_update(of=CrnSequence) if db.engine.dialect.name
           != 'sqlite' else
           CrnSequence.query.filter_by(bu=bu, kind=kind, fy=fy))
    row = row.first()
    if row is None:
        row = CrnSequence(bu=bu, kind=kind, fy=fy, last_serial=0)
        db.session.add(row)
        db.session.flush()
    row.last_serial = (row.last_serial or 0) + 1
    if row.last_serial >= 10 ** WIDTH:
        raise CrnError(
            f'{bu}-{kind}{fy} has reached {10 ** WIDTH - 1}. The series '
            f'is full; widening it changes every reference already '
            f'issued, so this needs a decision rather than a bigger '
            f'number.')
    db.session.flush()
    return format_crn(bu, kind, fy, row.last_serial)


def peek(bu, kind, *, fy=None, on=None):
    """What `allocate` would return, without taking it."""
    from app.models.ff import CrnSequence
    fy = fy or financial_year(on)
    row = CrnSequence.query.filter_by(
        bu=(bu or '').strip().upper(), kind=kind, fy=fy).first()
    return format_crn(bu, kind, fy, (row.last_serial if row else 0) + 1)


def subject_tag(crn):
    """How a CRN appears in a subject line.

    Square brackets, because mail clients keep them through replies
    and forwards where other punctuation gets mangled, and because
    `Re: Fwd: [PWL-T26-000014] ...` still parses.
    """
    return f'[{crn}]'
