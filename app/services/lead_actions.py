"""
What the CRM already knows, because the CRM did it.

CRM-COCKPIT-02 §5.3: "sending an intro, profile or quote from the CRM
sets the matching date, moves the stage and sets the next follow-up.
No manual stage update for actions done in the CRM."

The gap this closes is small to describe and large in practice. A
quotation went out of this CRM, as a PDF, through the outbox, against
a quote record that holds the number, the value, the date and the
validity — and the lead it belonged to stayed at RFQ Generated with
every quote field empty, until somebody typed the same four figures in
by hand. That is why 137 leads at quoting stages had 28 quote dates
between them, and it is why §9.9 is an acceptance check.

Nothing here guesses. Each of these is called at the moment the CRM
performs the action, and writes what the action itself carried.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

log = logging.getLogger(__name__)

#: §5.3 — what each action implies about the stage. A stage is only
#: ever moved forward; a quote sent on a lead already Won does not
#: drag it back to Quoted.
#:
#: 'Call Done' and not 'Contacted': Contacted is the lead strip's
#: label for a group of stages, not a stage the journey knows, and
#: `stage_map.position()` answers None for it — so a rule written
#: against it would silently never fire. 'Call Done' is the journey's
#: own first-contact stage and what 'Profile Sent' is an alias of.
INTRO_STAGE = 'Call Done'
QUOTE_STAGE = 'Quoted'


def _forward_only(lead, to):
    """Move the stage only if `to` is further along than it is now."""
    from app.services import stage_map

    here = stage_map.position(stage_map.canonical(lead.stage or ''))
    there = stage_map.position(stage_map.canonical(to))
    if there is None:
        return False
    if (lead.stage or '') in stage_map.TERMINAL:
        return False        # a closed deal is not reopened by a mail
    if here is not None and here >= there:
        return False
    lead.stage = to
    lead.stage_entered_at = datetime.utcnow()
    return True


def _set_followup(lead, *, actor=None):
    from app.services import followup

    wanted = followup.propose(lead)
    if wanted is not None and lead.followup_date != wanted:
        lead.followup_date = wanted
        return wanted
    return lead.followup_date


def quote_sent(lead, quote, *, actor=None, when=None):
    """A quotation went to the customer (§5.3, §9.9).

    Fills the lead's quote number, value, date and validity from the
    quote record, moves the stage to Quoted and sets the next
    follow-up. Returns what it changed.
    """
    if lead is None or quote is None:
        return {}
    from app.services import audit, last_activity as la

    when = when or datetime.utcnow()
    changed = {}

    def put(field, value):
        if value in (None, '') or getattr(lead, field, None) == value:
            return
        setattr(lead, field, value)
        changed[field] = value

    put('quote_no', getattr(quote, 'quote_number', None))
    put('quote_value_num', getattr(quote, 'total_amount', None))
    put('quote_date', (getattr(quote, 'quote_date', None)
                       or when.date()))
    put('quote_validity_date', getattr(quote, 'validity_until', None))
    put('quote_recorded_by', actor)
    put('quote_recorded_at', when)

    if _forward_only(lead, QUOTE_STAGE):
        changed['stage'] = QUOTE_STAGE
    followup_date = _set_followup(lead, actor=actor)
    if followup_date:
        changed['followup_date'] = followup_date

    # A quote is in, so the deadline is no longer a question — and if
    # the lead was flagged as a miss on a deadline it actually met,
    # the flag goes (§2.7's tile counts unanswered misses).
    if getattr(lead, 'quote_due_missed_at', None) is not None:
        lead.quote_due_missed_at = None
        changed['quote_due_missed_at'] = None

    la.touch(lead, la.QUOTE, when=when)
    audit.record('lead.quote_sent', 'lead', lead.id,
                 new={k: str(v) for k, v in changed.items()},
                 actor=actor,
                 reason=f'quotation {getattr(quote, "quote_number", "")} '
                        f'sent from the CRM',
                 commit=False)
    log.info('lead %s updated from quote %s: %s', lead.id,
             getattr(quote, 'quote_number', '?'), sorted(changed))
    return changed


def intro_sent(lead, *, kind='intro', actor=None, when=None):
    """An introduction or a profile went out (§5.3).

    `kind` is 'intro' or 'profile'; both set their own date, move a
    New lead to Contacted and set the next follow-up.
    """
    if lead is None:
        return {}
    from app.services import audit, last_activity as la

    when = when or datetime.utcnow()
    changed = {}
    field = 'intro_mail_date'
    if getattr(lead, field, None) is None:
        setattr(lead, field, when.date())
        changed[field] = when.date()
    if kind == 'profile' and hasattr(lead, 'sub_status') \
            and not (lead.sub_status or '').strip():
        lead.sub_status = 'Profile sent'
        changed['sub_status'] = 'Profile sent'
    if _forward_only(lead, INTRO_STAGE):
        changed['stage'] = INTRO_STAGE
    followup_date = _set_followup(lead, actor=actor)
    if followup_date:
        changed['followup_date'] = followup_date
    la.touch(lead, la.MAIL_OUT, when=when)
    audit.record('lead.intro_sent', 'lead', lead.id,
                 new={k: str(v) for k, v in changed.items()},
                 actor=actor, reason=f'{kind} sent from the CRM',
                 commit=False)
    return changed


def lead_of_quote(quote):
    """The lead a quote belongs to, directly or through its RFQ."""
    from app import Lead, db

    lead_id = getattr(quote, 'lead_id', None)
    if not lead_id:
        rfq_id = getattr(quote, 'rfq_id', None)
        if rfq_id:
            try:
                from app.models.rfq import RFQ
                rfq = db.session.get(RFQ, rfq_id)
                lead_id = getattr(rfq, 'lead_id', None)
            except Exception:                             # noqa: BLE001
                lead_id = None
    if not lead_id:
        return None
    return db.session.get(Lead, lead_id)


# ── §5.4 the quote reader ────────────────────────────────────────────
#: What a quotation document states, however it is laid out. Kept here
#: rather than in the PDF code because an Excel quote, a forwarded
#: offer and a PDF all state the same four things.
import re                                                 # noqa: E402

#: The label is required, not optional. Without it, "PROCAM
#: LOGISTICS — QUOTATION / Quotation No: PQ-2026-0099" matched
#: QUOTATION in the letterhead and captured the word "Quotation" as
#: the quote number. A quote number is always labelled, so insisting
#: on the label costs nothing and removes the whole failure.
RX_NUMBER = re.compile(
    r'\b(?:quot(?:e|ation)|ref(?:erence)?)\s*'
    r'(?:no\.?|number|#|ref)\s*[:\-]?\s*'
    r'([A-Z0-9][A-Z0-9/\-]{3,24})\b', re.I)
RX_TOTAL = re.compile(
    r'\b(?:grand\s*total|total\s*(?:amount|value)?|net\s*payable)\b'
    r'[^0-9₹]{0,20}(?:₹|INR|Rs\.?)?\s*'
    r'([0-9][0-9,]{2,15}(?:\.[0-9]{1,2})?)', re.I)
RX_VALID = re.compile(
    r'\b(?:valid(?:ity)?\s*(?:up\s*to|until|till)?|offer\s+valid)\b'
    r'[^0-9]{0,24}(\d{1,2}[\s\-/][A-Za-z]{3,9}[\s\-/]\d{2,4}'
    r'|\d{1,2}[\-/.]\d{1,2}[\-/.]\d{2,4})', re.I)
RX_DATE = re.compile(
    r'\b(?:quot(?:e|ation)\s*date|dated)\b[^0-9]{0,16}'
    r'(\d{1,2}[\s\-/][A-Za-z]{3,9}[\s\-/]\d{2,4}'
    r'|\d{1,2}[\-/.]\d{1,2}[\-/.]\d{2,4})', re.I)


def read_quote_document(text):
    """{quote_no, value, quote_date, validity} from a quote's text.

    §5.4. Returns only what it is sure of: a number it could not find
    is absent from the result rather than guessed, and the owner
    confirms whatever is there.
    """
    from app.services import quote_due as qd

    text = ' '.join((text or '').split())
    if not text:
        return {}
    out = {}
    m = RX_NUMBER.search(text)
    if m:
        out['quote_no'] = m.group(1).upper()[:40]
    m = RX_TOTAL.search(text)
    if m:
        try:
            out['value'] = float(m.group(1).replace(',', ''))
        except ValueError:
            pass
    for key, rx in (('quote_date', RX_DATE), ('validity', RX_VALID)):
        m = rx.search(text)
        if not m:
            continue
        day, _end = qd._date_at(m.group(1))
        if day is not None:
            out[key] = day
    return out


def apply_quote_reading(lead, reading, *, actor=None):
    """Put a document's figures on the lead, unconfirmed (§5.4).

    Blanks only, like every other autofill: a figure the owner has
    typed is not replaced by one read off a PDF.
    """
    from app.services import autofill as af

    if not reading:
        return {}
    pairs = (('quote_no', reading.get('quote_no')),
             ('quote_value_num', reading.get('value')),
             ('quote_date', reading.get('quote_date')),
             ('quote_validity_date', reading.get('validity')))
    filled = {}
    for field, value in pairs:
        if value in (None, ''):
            continue
        if getattr(lead, field, None) not in (None, '', 0):
            continue
        setattr(lead, field, value)
        filled[field] = value
    if filled:
        # Marked like any other reading, so the screen says "auto —
        # confirm" against a figure nobody has checked.
        for field in filled:
            af.mark_auto(lead, field)
    return filled
