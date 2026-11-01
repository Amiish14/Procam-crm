"""
When did anything last happen on this lead?

CRM-COCKPIT-02 §4.1. The cockpit has three tiles — No activity 7D, 15D
and 30D — and all three read zero for every lead in the CRM, for ever,
while the ageing chart beside them showed 584 leads sitting at 16–30
days. Both numbers came off the same records. The tiles were counting
something that was not written down.

`updated_at` could not stand in for it: one bulk edit touched 2,381
leads in a day, so "last updated" says the whole book was worked on
that Tuesday. The audit trail knows, but it is one row per change
rather than one per lead, so every tile became a scan of 60,000 audit
rows to answer "how long has this been quiet".

So the lead carries it, and this module is the only thing that writes
it. Two ways in, deliberately:

  * `touch(lead, kind)` for a caller that knows it just did something;
  * a session listener for every caller that does not — a note saved
    by a script, a mail filed by the ingest, a stage moved by a bulk
    tool. §4.1 lists seven kinds of activity and this CRM has more
    than seven ways to create each one, so the rule lives at the
    session rather than in the routes.

`last_activity_kind` rides along, because "quiet for 20 days" and
"quiet for 20 days, and the last thing was our own mail going out"
are different conversations with the owner.
"""
from __future__ import annotations

import logging
from datetime import datetime

log = logging.getLogger(__name__)

MAIL_IN = 'mail_in'
MAIL_OUT = 'mail_out'
NOTE = 'note'
CALL = 'call'
MEETING = 'meeting'
QUOTE = 'quote'
STAGE = 'stage'
TASK = 'task'
CREATED = 'created'
KINDS = (MAIL_IN, MAIL_OUT, NOTE, CALL, MEETING, QUOTE, STAGE, TASK,
         CREATED)

#: LeadActivity.kind → what we call it. The activity table's own
#: vocabulary is older and looser than §4.1's list.
FROM_ACTIVITY_KIND = {
    'call': CALL, 'phone': CALL, 'email': MAIL_OUT, 'mail': MAIL_OUT,
    'meeting': MEETING, 'visit': MEETING, 'note': NOTE, 'rfq': QUOTE,
    'quote': QUOTE,
}


def touch(lead, kind=NOTE, *, when=None, only_forward=True):
    """Stamp a lead as worked on. Returns the moment written, or None.

    Never moves the stamp backwards by default: filing an old mail
    against a lead is not the lead going quiet again, and a back-fill
    that reads history out of order would otherwise leave whichever
    row it happened to see last.
    """
    if lead is None:
        return None
    when = when or datetime.utcnow()
    now = getattr(lead, 'last_activity_at', None)
    if only_forward and now is not None and when <= now:
        return None
    try:
        lead.last_activity_at = when
        lead.last_activity_kind = (kind or NOTE)[:20]
    except Exception:                                     # noqa: BLE001
        log.exception('could not stamp last activity on lead %s',
                      getattr(lead, 'id', '?'))
        return None
    return when


def touch_id(lead_id, kind=NOTE, *, when=None):
    """The same, for a caller holding an id rather than a lead."""
    if not lead_id:
        return None
    from app import Lead, db

    lead = db.session.get(Lead, int(lead_id))
    return touch(lead, kind, when=when)


def quiet_days(lead, *, now=None):
    """How long since anything happened, in days, or None.

    Falls back to the created date rather than answering None, which
    is §4.1's own rule: a lead nobody has ever touched has been quiet
    since it arrived, and that is the lead the tile most wants to
    show.
    """
    at = (getattr(lead, 'last_activity_at', None)
          or getattr(lead, 'received_at', None)
          or getattr(lead, 'created_at', None))
    if at is None:
        return None
    return ((now or datetime.utcnow()) - at).days


# ── the session listener ─────────────────────────────────────────────
#: Model name → how to read (lead_id, kind, when) off a new row.
#: Only inserts are watched: editing a note's text is not a new
#: activity, and a deletion is not activity at all.
_NEW_ROW = {
    'LeadActivity': lambda row: (
        getattr(row, 'lead_id', None),
        FROM_ACTIVITY_KIND.get((getattr(row, 'kind', '') or '').lower(),
                               NOTE),
        getattr(row, 'occurred_at', None)),
    'LeadNote': lambda row: (
        getattr(row, 'lead_id', None), NOTE,
        getattr(row, 'created_at', None)),
    'LeadEmail': lambda row: (
        getattr(row, 'lead_id', None),
        (MAIL_OUT if (getattr(row, 'direction', '') or '').lower()
         in ('out', 'outbound', 'sent') else MAIL_IN),
        getattr(row, 'sent_at', None) or getattr(row, 'received_at', None)),
    'LeadStageHistory': lambda row: (
        getattr(row, 'lead_id', None), STAGE,
        getattr(row, 'changed_at', None)),
    'LeadQuoteDueHistory': lambda row: (
        getattr(row, 'lead_id', None), QUOTE,
        getattr(row, 'created_at', None)),
}

#: Lead fields whose change is itself activity, and what to call it.
_LEAD_FIELDS = {
    'stage': STAGE,
    'quote_no': QUOTE,
    'quote_value_num': QUOTE,
    'quote_date': QUOTE,
    'phone_call_date': CALL,
    'meeting_date': MEETING,
    'intro_mail_date': MAIL_OUT,
}


def install():
    """Attach the listener. Called once, from app.py."""
    from sqlalchemy import event, inspect
    from sqlalchemy.orm import Session

    @event.listens_for(Session, 'before_flush')
    def _stamp(session, _flush_context, _instances):     # noqa: ANN001
        # Collected first and applied after, so stamping a lead
        # cannot change the set being iterated.
        wanted = {}

        def want(lead_id, kind, when):
            if not lead_id:
                return
            when = when or datetime.utcnow()
            have = wanted.get(lead_id)
            if have is None or when > have[1]:
                wanted[lead_id] = (kind, when)

        for row in session.new:
            # A lead arriving is itself the first activity on it
            # (§4.1: "if nothing exists, use the created date"). Set
            # here rather than left to the tile's fallback, so a
            # query can filter on the column instead of every reader
            # having to remember the fallback.
            if type(row).__name__ == 'Lead':
                if getattr(row, 'last_activity_at', None) is None:
                    touch(row, CREATED,
                          when=(getattr(row, 'received_at', None)
                                or getattr(row, 'created_at', None)
                                or datetime.utcnow()))
                continue
            reader = _NEW_ROW.get(type(row).__name__)
            if reader is None:
                continue
            try:
                want(*reader(row))
            except Exception:                             # noqa: BLE001
                continue

        for row in session.dirty:
            if type(row).__name__ != 'Lead':
                continue
            try:
                state = inspect(row)
            except Exception:                             # noqa: BLE001
                continue
            for field, kind in _LEAD_FIELDS.items():
                try:
                    history = state.attrs[field].history
                except Exception:                         # noqa: BLE001
                    continue
                if not history.has_changes():
                    continue
                # The lead in hand — stamp it directly rather than
                # loading it again by id mid-flush.
                touch(row, kind)
                break

        if not wanted:
            return
        from app import Lead

        # `no_autoflush` matters: loading a lead from inside
        # before_flush would otherwise start a second flush, and the
        # listener would be re-entered while the first one is still
        # collecting.
        with session.no_autoflush:
            for lead_id, (kind, when) in wanted.items():
                try:
                    lead = session.get(Lead, lead_id)
                except Exception:                         # noqa: BLE001
                    continue
                if lead is None:
                    continue
                touch(lead, kind, when=when)
