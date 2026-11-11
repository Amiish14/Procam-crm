"""The customer is chasing us. Record it, and say so on the lead.

CRM-MAIL-2026-11-11 §3+§5: a GE Vernova reminder arrived asking for
the D&D estimate "as of 15 Nov instead of 5 Nov". It became a second
lead. The threading fix (`lead_intake.strip_lead_tags`) is what stops
that; this is the other half of what §3+§5 asks for — "attach to the
existing lead's trail and flag it customer chasing".

WHY A FLAG AT ALL, WHEN THE MAIL IS ON THE TRAIL

Because nobody reads a trail to find out that nothing has happened.
A chase is the one inbound message that is not information about the
enquiry — it is information about *us*: the customer has asked twice
and is still waiting. On a board of two hundred open leads that is
the single most useful thing the CRM can know, and it was arriving
as one more row in a list.

So two columns on the lead: when the chasing started and how many
times. `chasing_since` is the first chase that was not answered;
`chase_count` is how many have arrived. They are cleared when we
send the customer something, which is what answering means here.

WHAT COUNTS AS A CHASE

Inbound, from the lead's own customer, and either a reminder in so
many words or a reply that asks again for something with a date on
it. Deliberately narrow: a customer sending a revised BOQ is not
chasing, and marking it as a chase would turn the flag into noise —
which is the one failure that would make everybody ignore it.

Our own mail never counts, however it is worded: "gentle reminder"
from us is us chasing them.
"""
from __future__ import annotations

import re

ACTION = 'lead.customer_chasing'

#: The wording a customer uses when asking again. Each is a phrase
#: rather than a word: "status" alone is in half the subjects in the
#: mailbox.
CHASE_PATTERNS = (
    r'\bgentle\s+reminder\b',
    r'\breminder\b',
    r'\bfollow[\s-]?up\b',
    r'\bfollowing up\b',
    r'\bany\s+update\b',
    r'\bawait(?:ing|ed)\b.{0,30}\b(?:quote|quotation|offer|rate|reply|'
    r'response|revert)\b',
    r'\bstill\s+(?:awaiting|waiting|not received)\b',
    r'\bnot\s+(?:yet\s+)?received\b.{0,30}\b(?:quote|quotation|offer|rate)\b',
    r'\bkindly\s+(?:expedite|revert|share|confirm)\b',
    r'\bplease\s+(?:expedite|revert|treat (?:this )?as urgent)\b',
    r'\bsecond\s+reminder\b',
    r'\b(?:1st|2nd|3rd|first|second|third)\s+reminder\b',
    r'\brevert\s+(?:at the earliest|urgently|asap)\b',
    r'\bas\s+discussed\b.{0,40}\bpending\b',
    r'\bpending\s+(?:from your end|at your end)\b',
)

_CHASE = tuple(re.compile(p, re.I) for p in CHASE_PATTERNS)

#: How much of a body to read. A chase says so at the top; the rest
#: is the quoted thread, which contains everything ever written.
_BODY_HEAD = 1200


def reads_as_a_chase(subject='', body=''):
    """The phrase that makes this a chase, or ''.

    The subject in full, and only the head of the body: the quoted
    chain below a reply holds every word anybody has used, including
    'reminder' from a month ago.
    """
    text = f"{subject or ''}\n{(body or '')[:_BODY_HEAD]}"
    for pattern in _CHASE:
        found = pattern.search(text)
        if found:
            return found.group(0).strip().lower()
    return ''


def is_a_chase(lead, *, subject='', body='', from_addr='',
               direction='inbound'):
    """Whether this message is the customer chasing this lead.

    All three have to hold: inbound, from the customer, and worded as
    a chase. `mail_filing.from_the_customer` answers the second one —
    the same rule the Email trail uses, so the flag and the trail
    cannot disagree about who a message is from.
    """
    if lead is None or (direction or 'inbound') == 'outbound':
        return ''
    try:
        from app.services import mail_filing
        probe = type('Row', (), {'direction': 'inbound',
                                 'from_addr': from_addr,
                                 'intake_class': ''})()
        if not mail_filing.from_the_customer(lead, probe):
            return ''
    except Exception:                                     # noqa: BLE001
        return ''
    return reads_as_a_chase(subject, body)


def flag(lead, *, phrase, when=None, actor='system', message_id=None):
    """Record a chase on the lead. Idempotent per message.

    Never raises: a flag that could break the ingest would cost the
    message, and the message matters more than the flag.
    """
    from datetime import datetime

    from app import db

    if lead is None or not phrase:
        return False
    when = when or datetime.utcnow()
    try:
        if message_id and _already(lead, message_id):
            return False
        if getattr(lead, 'chasing_since', None) is None:
            lead.chasing_since = when
        lead.chase_count = int(getattr(lead, 'chase_count', 0) or 0) + 1
        from app.services import audit
        audit.record(ACTION, 'lead', lead.id,
                     new={'phrase': phrase, 'count': lead.chase_count,
                          'message_id': message_id or ''},
                     actor=actor,
                     reason=(f'the customer is chasing: "{phrase}" '
                             f'(chase {lead.chase_count})')[:400])
        db.session.flush()
        return True
    except Exception:                                     # noqa: BLE001
        try:
            db.session.rollback()
        except Exception:                                 # noqa: BLE001
            pass
        return False


def _already(lead, message_id):
    """Has this exact message already been counted?

    Graph re-delivers a notification when the webhook answers slowly,
    and a chase counted three times reads as three chases.
    """
    try:
        from app.models.audit import AuditEvent
        rows = (AuditEvent.query
                .filter(AuditEvent.action == ACTION,
                        AuditEvent.entity_type == 'lead',
                        AuditEvent.entity_id == str(lead.id))
                .order_by(AuditEvent.occurred_at.desc()).limit(25).all())
    except Exception:                                     # noqa: BLE001
        return False
    for row in rows:
        payload = row.new_value if isinstance(row.new_value, dict) else {}
        if (payload.get('message_id') or '') == message_id:
            return True
    return False


def answered(lead, *, actor='system'):
    """We have written to the customer: the chase is answered.

    Clears both columns. Called from the one place a lead's outbound
    mail is recorded, so "we replied" and "the flag went out" are the
    same event rather than two that can drift.
    """
    from app import db

    if lead is None:
        return False
    if not (getattr(lead, 'chasing_since', None)
            or getattr(lead, 'chase_count', 0)):
        return False
    try:
        from app.services import audit
        audit.record('lead.chase_answered', 'lead', lead.id,
                     old={'chase_count': int(lead.chase_count or 0),
                          'chasing_since': str(lead.chasing_since or '')},
                     new={'chase_count': 0},
                     actor=actor,
                     reason='we wrote to the customer')
        lead.chasing_since = None
        lead.chase_count = 0
        db.session.flush()
        return True
    except Exception:                                     # noqa: BLE001
        return False


def state(lead):
    """What the lead screen shows, or None."""
    if lead is None or not getattr(lead, 'chase_count', 0):
        return None
    from datetime import datetime

    since = getattr(lead, 'chasing_since', None)
    days = (datetime.utcnow() - since).days if since else None
    return {
        'count': int(lead.chase_count or 0),
        'since': str(since)[:19] if since else '',
        'days': days,
        'label': ('The customer is chasing'
                  + (f' — {lead.chase_count} times' if (lead.chase_count or 0) > 1
                     else '')
                  + (f', first asked {days} day(s) ago' if days else '')),
    }
