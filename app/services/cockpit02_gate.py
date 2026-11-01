"""
Two dates the CRM now insists on, and the one place that decides.

CRM-COCKPIT-02 §2.2 and §3.1:

  * from RFQ Generated until a quote is recorded, a lead must carry a
    quote submission due date;
  * every stage change and every activity save must leave a next
    follow-up date behind, except on Won, Lost and On Hold.

Why one module rather than a check in each route: there are four ways
to change a lead's stage in this CRM (the lead sheet's PUT, the v2
strip, the advance endpoint and the bulk screens) and five ways to log
an activity. A rule enforced in nine places is a rule with nine ways
to be wrong, and the one that matters here is the way where the rule
quietly does not apply on the screen most people use.

Every refusal comes back as a guided-help refusal — the code, the
reason, the steps and a [Take me there] button — because §0.5 asks for
it and because "Quote due date is required" on its own tells somebody
nothing they can act on.

What this does NOT do is refuse a disposal. Closing a lead as Lost,
marking it a duplicate and Delete – not relevant stay open with no
dates filled in: a lead must always be disposable, or people work
around the CRM instead of in it.
"""
from __future__ import annotations

from datetime import date, datetime

#: §2.2 — mandatory from this stage onwards.
DUE_FROM = 'RFQ Generated'

#: §3.1 — these never ask for a follow-up.
FOLLOWUP_EXEMPT = ('Won', 'Lost', 'On Hold', 'Not Interested')

QUOTE_DUE_MISSING = 'QUOTE_DUE_MISSING'
FOLLOWUP_MISSING = 'FOLLOWUP_MISSING'


def _position(stage):
    """Where a stage sits on the journey, or -1 if it is not on it.

    `stage_map.position()` answers None for a stage it does not know,
    and this CRM still carries old values — 'Meeting', 'Profile Sent'
    before it became an alias, whatever an import wrote in 2024.
    Comparing None with an int raised a TypeError, which came back to
    the user as a 500 on an ordinary save: the gate meant to protect a
    deadline would have broken the save of every lead carrying a
    retired stage name.
    """
    from app.services import stage_map

    where = stage_map.position(stage_map.canonical(stage or ''))
    return -1 if where is None else where


def _at_or_after_due_from(stage):
    """Is this stage RFQ Generated or later on the journey?

    Terminal stages answer False: a lead being closed is not a lead
    being asked for a deadline.
    """
    stage = (stage or '').strip()
    from app.services import stage_map

    if stage_map.canonical(stage) in stage_map.TERMINAL:
        return False
    here, from_ = _position(stage), _position(DUE_FROM)
    return here >= 0 and from_ >= 0 and here >= from_


def quote_recorded(lead, payload=None):
    """Has an offer gone in? Any of the quote fields counts."""
    payload = payload if isinstance(payload, dict) else {}
    for key in ('quote_no', 'quote_date', 'quote_value_num',
                'quote_value'):
        if str(payload.get(key) or '').strip():
            return True
    return bool((getattr(lead, 'quote_no', '') or '').strip()
                or getattr(lead, 'quote_date', None)
                or (getattr(lead, 'quote_value_num', None) or 0))


def _due_in(lead, payload):
    """The due date this save will leave on the lead, if any."""
    payload = payload if isinstance(payload, dict) else {}
    for key in ('quote_due_at', 'quote_due', 'quote_due_date'):
        raw = payload.get(key)
        if raw:
            return raw
    return getattr(lead, 'quote_due_at', None)


def _followup_in(lead, payload):
    payload = payload if isinstance(payload, dict) else {}
    for key in ('followup', 'followup_date', 'next_followup'):
        raw = payload.get(key)
        if raw:
            return raw
    return getattr(lead, 'followup_date', None)


# ── the two questions ────────────────────────────────────────────────
def quote_due_needed(lead, stage=None, payload=None):
    """True when this save must carry a quote submission due date."""
    stage = stage if stage is not None else getattr(lead, 'stage', '')
    if not _at_or_after_due_from(stage):
        return False
    if quote_recorded(lead, payload):
        return False        # §2.2 — mandatory only until a quote exists
    return not _due_in(lead, payload)


def followup_needed(lead, stage=None, payload=None):
    """True when this save must leave a next follow-up date."""
    stage = stage if stage is not None else getattr(lead, 'stage', '')
    from app.services import stage_map

    if stage_map.canonical(stage or '') in FOLLOWUP_EXEMPT:
        return False
    if (stage or '').strip() in FOLLOWUP_EXEMPT:
        return False
    return not _followup_in(lead, payload)


def context_for(lead, code, stage=None):
    """What the guide needs to draw its buttons."""
    from app.services import followup, quote_due as qd

    ctx = {
        'lead_id': getattr(lead, 'id', None),
        'company': getattr(lead, 'company', '') or '',
        'stage': (stage or getattr(lead, 'stage', '') or ''),
    }
    if code == QUOTE_DUE_MISSING:
        # Offer what the mail says, so [Take me there] lands on a
        # filled box rather than an empty one.
        try:
            reading = qd.read_lead(lead)
        except Exception:                                 # noqa: BLE001
            reading = None
        ctx['field'] = 'quote_due_at'
        ctx['suggested'] = qd.shown(reading.at) if reading else ''
        ctx['suggested_iso'] = (reading.at.isoformat(timespec='minutes')
                                if reading else '')
        ctx['suggested_source'] = reading.source if reading else ''
    else:
        try:
            proposed = followup.propose(lead, stage=stage)
        except Exception:                                 # noqa: BLE001
            proposed = None
        ctx['field'] = 'followup'
        ctx['suggested'] = proposed.isoformat() if proposed else ''
        ctx['suggested_iso'] = ctx['suggested']
        ctx['quick_picks'] = [
            {'key': key, 'label': label, 'date': value.isoformat()}
            for key, label, value in followup.quick_picks(lead)]
    return ctx


def refusal(lead, *, stage=None, payload=None, http=400):
    """(body, status) when a save must be refused, else None.

    The order matters: a lead at RFQ Generated with neither date gets
    the due-date guide first, because the follow-up at that stage is
    computed from the due date and asking for it first would ask the
    same person the same question twice.
    """
    from app.services import guided_help

    stage = stage if stage is not None else getattr(lead, 'stage', '')
    for code, needed in ((QUOTE_DUE_MISSING, quote_due_needed),
                         (FOLLOWUP_MISSING, followup_needed)):
        if needed(lead, stage, payload):
            body, status = guided_help.refusal(
                code, context_for(lead, code, stage), http=http)
            # The field the screen has to open, under the key every
            # other gate in this CRM uses, so the existing form code
            # needs no new branch.
            body['needs'] = ('quote_due_at'
                             if code == QUOTE_DUE_MISSING else 'followup')
            body['suggested'] = body['guide']['context'].get('suggested', '')
            return body, status
    return None


def error_for(lead, *, stage=None, payload=None):
    """The same decision for a caller that only wants a sentence.

    `lead_flow.may_move` and the bulk screens report missing fields
    rather than returning a response, so they get the words.
    """
    stage = stage if stage is not None else getattr(lead, 'stage', '')
    out = []
    if quote_due_needed(lead, stage, payload):
        out.append({'field': 'quote_due_at',
                    'label': 'Quote submission due date',
                    'tab': 'summary'})
    if followup_needed(lead, stage, payload):
        out.append({'field': 'followup_date',
                    'label': 'Next follow-up date',
                    'tab': 'summary'})
    return out


# ── writing the dates a save carries ─────────────────────────────────
def parse_due(raw):
    """A due date from a form, in IST, stored as UTC.

    Accepts what an `<input type="datetime-local">` sends
    ('2026-11-04T18:00'), a date on its own, and a space instead of
    the T. A date with no time is taken as the assumed hour rather
    than midnight — midnight would mark a deadline as missed from the
    moment it was typed.
    """
    from app.services import quote_due as qd

    if raw in (None, ''):
        return None
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, date):
        return datetime.combine(raw, qd.ASSUMED_HOUR) - qd.IST
    text = str(raw).strip().replace('T', ' ')
    if not text:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d',
                '%d-%m-%Y %H:%M', '%d/%m/%Y %H:%M', '%d-%m-%Y',
                '%d/%m/%Y'):
        try:
            value = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y'):
            value = datetime.combine(value.date(), qd.ASSUMED_HOUR)
        return value - qd.IST
    raise ValueError(f'{raw!r} is not a date and time I can read')


def apply_due(lead, raw, *, actor=None, source=None, reason_code='set',
              reason_text='', email_message_id=None, confirmed=True):
    """Set or move a lead's due date, with its history row (§2.4).

    Returns the history row, or None when nothing changed. Does not
    commit: the caller owns the transaction, as every other service
    in this CRM does.
    """
    from app import db
    from app.models.cockpit02 import LeadQuoteDueHistory

    value = raw if isinstance(raw, datetime) or raw is None \
        else parse_due(raw)
    was = getattr(lead, 'quote_due_at', None)
    if was == value:
        if confirmed and not getattr(lead, 'quote_due_confirmed', False):
            lead.quote_due_confirmed = True
            return None
        return None

    lead.quote_due_at = value
    lead.quote_due_source = source or ('user' if actor else None)
    lead.quote_due_confirmed = bool(confirmed)
    if was is not None and value is not None and reason_code != 'set':
        lead.quote_due_extensions = (lead.quote_due_extensions or 0) + 1
    # A date that has moved forward is no longer missed; the sweep
    # will say so again if the new one passes too.
    if value is not None and lead.quote_due_missed_at is not None \
            and value > datetime.utcnow():
        lead.quote_due_missed_at = None

    row = LeadQuoteDueHistory(
        lead_id=lead.id, old_due_at=was, new_due_at=value,
        reason_code=reason_code or 'set',
        reason_text=(reason_text or '')[:300],
        source=source, email_message_id=email_message_id,
        actor=actor)
    db.session.add(row)

    # §3.2 — at RFQ Generated the follow-up is a day before the
    # deadline, so a deadline that moves takes the follow-up with it.
    try:
        from app.services import followup
        followup.after_due_change(lead, actor=actor)
    except Exception:                                     # noqa: BLE001
        pass
    return row
