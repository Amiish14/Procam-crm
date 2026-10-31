"""
Which vertical a record belongs to, and whose vertical that is.

The gap this closes: `employees.vertical` holds four values —
Heavy Transport, Installation, Warehousing, Project Freight — and
leads carry nine, including Freight Forwarding, Customs, Heavy Cargo
and General Transport. Nothing mapped between them. So a lead in six
of those nine verticals resolved to no vertical head, and the CC that
was meant to reach a head reached nobody at all. Silently, because a
missing recipient is not an error.

Two tables fix it and an administrator maintains both:
`vertical_alias` says which of the nine means which of the four, and
`vertical_head` says whose each one is.

Nothing here falls back to guessing. An unmapped vertical resolves to
Management — somebody senior is copied — and the unmapped value is
reported in the administrators' morning email, so the gap is visible
rather than quiet.
"""
from __future__ import annotations

import logging
from typing import NamedTuple

from app.models.email_admin import FALLBACK_VERTICAL

log = logging.getLogger(__name__)

#: Rebuilt when the master changes. The resolver runs on every
#: notification, and these are a handful of rows that change a few
#: times a year — but the cache expires, because two gunicorn workers
#: do not see each other's clear and a head changed on one worker
#: must reach the other. Thirty seconds, the same as the block
#: register, for the same reason.
_CACHE_TTL_SECONDS = 30
_CACHE = {'heads': None, 'aliases': None, 'at': 0.0}


class Head(NamedTuple):
    """One vertical's master row, as plain values.

    The cache used to hold the `VerticalHead` ORM instances
    themselves, and that is what made Save on Admin → Email →
    Verticals answer 500 while saving the change perfectly well
    (CRM-VERTICAL-SAVE-500-01):

        the save commits, which expires every attribute on the
        instances it touched;
        it then clears this cache — but `sync_employee_flags()`,
        which runs next, calls `_load()` again and refills the cache
        with instances from that same request's session;
        the request ends and Flask-SQLAlchemy removes the session,
        so those instances are now detached as well as expired;
        the redirect lands on the screen, `gaps()` reads
        `row.head_emp_code` off one of them within the 30-second
        TTL, and SQLAlchemy raises DetachedInstanceError.

    A tuple of strings cannot be expired and cannot be detached, so
    the whole class of failure goes with it. The attribute names are
    the ORM's, deliberately: every caller reads them unchanged.
    """
    vertical: str
    head_emp_code: str
    deputy_emp_code: str
    active: bool


def cache_clear():
    _CACHE.update({'heads': None, 'aliases': None, 'at': 0.0})


def normalise(value):
    """An alias as it is compared: lower case, space collapsed."""
    return ' '.join((value or '').strip().lower().split())


def _load(now=None):
    import time

    from app.models.email_admin import VerticalAlias, VerticalHead

    now = now if now is not None else time.monotonic()
    if _CACHE['heads'] is not None and (now - _CACHE['at']) < _CACHE_TTL_SECONDS:
        return _CACHE['heads'], _CACHE['aliases']
    try:
        heads = {h.vertical: Head(vertical=h.vertical,
                                  head_emp_code=h.head_emp_code or '',
                                  deputy_emp_code=h.deputy_emp_code or '',
                                  active=bool(h.active is not False))
                 for h in VerticalHead.query.filter(
                     VerticalHead.active.isnot(False)).all()}
        aliases = {normalise(a.alias): a.vertical
                   for a in VerticalAlias.query.all()}
    except Exception:
        # The tables arrive in a migration. Until then nothing is
        # mapped, which is the behaviour that existed before — not
        # cached, so the first request after the migration is right.
        return {}, {}
    _CACHE.update({'heads': heads, 'aliases': aliases, 'at': now})
    return heads, aliases


def master_vertical(raw):
    """The managed vertical a record's own value belongs to.

    An exact master name passes through. Anything else goes through
    the alias table. Unmapped, blank and unknown all land on
    Management, which is a decision rather than an accident.
    """
    heads, aliases = _load()
    value = (raw or '').strip()
    if value in heads:
        return value
    mapped = aliases.get(normalise(value))
    if mapped:
        return mapped
    return FALLBACK_VERTICAL


def is_mapped(raw):
    """False for a value nobody has told the CRM about."""
    heads, aliases = _load()
    value = (raw or '').strip()
    return bool(value) and (value in heads
                            or normalise(value) in aliases)


def heads_for_vertical(raw):
    """[emp_code] for the head and deputy of this vertical.

    Order matters: the head first. A deputy with no head is reported
    as a gap rather than quietly standing in — somebody has to be
    accountable, and 'the deputy covers it' is a decision for a
    person to make on the screen.
    """
    heads, _aliases = _load()
    vertical = master_vertical(raw)

    def _codes(row):
        out = []
        for code in (getattr(row, 'head_emp_code', None),
                     getattr(row, 'deputy_emp_code', None)):
            code = (code or '').strip().upper()
            if code and code not in out:
                out.append(code)
        return out

    found = _codes(heads.get(vertical))
    if found:
        return found
    # A row that exists and names nobody is the same silence as no row
    # at all — which is the bug this whole release is about. Either
    # way the fallback answers, and `gaps()` still reports the vertical
    # so somebody fixes it rather than the fallback quietly carrying
    # it forever.
    if vertical != FALLBACK_VERTICAL:
        return _codes(heads.get(FALLBACK_VERTICAL))
    return []


def vertical_of(record):
    """The vertical a record carries, whatever the record is.

    A lead has `procam_vertical`. An account has `vertical`. A quote
    has neither and reaches its vertical through its account. Asking
    each caller to work that out is how half of them would get it
    wrong.
    """
    if record is None:
        return ''
    for attr in ('procam_vertical', 'vertical'):
        value = (getattr(record, attr, '') or '').strip()
        if value:
            return value
    # Through the account, where there is one.
    for attr in ('company_id', 'account_id'):
        account_id = getattr(record, attr, None)
        if not account_id:
            continue
        try:
            from app import Company
            account = Company.query.get(account_id)
            value = (getattr(account, 'vertical', '') or '').strip()
            if value:
                return value
        except Exception:
            pass
    # Through the lead, for an RFQ or a quote that hangs off one.
    lead_id = getattr(record, 'lead_id', None)
    if lead_id:
        try:
            from app import Lead
            lead = Lead.query.get(lead_id)
            return (getattr(lead, 'procam_vertical', '') or '').strip()
        except Exception:
            pass
    return ''


def resolve_vertical_head(record):
    """The head (and deputy) for whatever this record is about.

    The function the notification policy calls. Returns employee
    codes, in order, never raising — a notification that cannot work
    out a head still has to reach its owner.
    """
    try:
        return heads_for_vertical(vertical_of(record))
    except Exception:
        log.exception('could not resolve a vertical head')
        return []


# ── reporting the gaps ───────────────────────────────────────────────
def unmapped_verticals():
    """Values live records carry that map to nothing.

    Goes into the administrators' morning report. A vertical nobody
    has mapped is a vertical whose head is not being copied, and that
    is invisible from the outside.
    """
    from app import Company, Lead, db

    found = {}
    try:
        rows = (db.session.query(Lead.procam_vertical, db.func.count(Lead.id))
                .filter(Lead.is_archived.isnot(True))
                .group_by(Lead.procam_vertical).all())
        for value, count in rows:
            if value and not is_mapped(value):
                found[value] = found.get(value, 0) + count
    except Exception:
        pass
    try:
        rows = (db.session.query(Company.vertical, db.func.count(Company.id))
                .group_by(Company.vertical).all())
        for value, count in rows:
            if value and not is_mapped(value):
                found[value] = found.get(value, 0) + count
    except Exception:
        pass
    return sorted(found.items(), key=lambda kv: -kv[1])


def gaps():
    """Verticals with no head, for the screen's red warnings."""
    heads, _aliases = _load()
    return sorted(v for v, row in heads.items()
                  if not (row.head_emp_code or '').strip())


# ── keeping the employee flag in step ────────────────────────────────
def sync_employee_flags(actor=None):
    """Set `employees.is_vertical_head` from this master.

    The flag is read by code written before this table existed — the
    weekly head report, the access-matrix default, the escalation
    ladder. Keeping it in step means none of that has to change, and
    means the flag stops being edited by hand in two places.

    Returns (set, cleared).
    """
    from app import Employee, db

    heads, _aliases = _load()
    should_be = set()
    for row in heads.values():
        for code in (row.head_emp_code, row.deputy_emp_code):
            if (code or '').strip():
                should_be.add(code.strip().upper())

    set_now, cleared = [], []
    try:
        for emp in Employee.query.all():
            wanted = emp.emp_code in should_be
            if bool(emp.is_vertical_head) == wanted:
                continue
            emp.is_vertical_head = wanted
            (set_now if wanted else cleared).append(emp.emp_code)
        db.session.commit()
    except Exception:
        db.session.rollback()
        log.exception('could not sync the vertical-head flags')
        return [], []

    if set_now or cleared:
        try:
            from app.services import audit
            audit.record('vertical_head.flags_synced', 'Employee', None,
                         new={'set': set_now, 'cleared': cleared},
                         actor=actor,
                         reason='kept employees.is_vertical_head in step '
                                'with the vertical head master')
        except Exception:
            pass
    return set_now, cleared
