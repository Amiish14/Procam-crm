"""Procurement portals: a real RFQ, from a machine. TMS of nothing — CRM.

CRM-FINAL-13NOV follow-up, item 1. In fourteen days 68 messages from
`procol.in` and 4 from `ansmtp.ariba.com` were dropped at intake with
"blocked sender domain". Both are **customer procurement portals**:
what they send is an RFQ or a tender invitation, from the buyer, to us.

HOW THEY CAME TO BE BLOCKED, AND WHY THAT WAS ALREADY ARGUED AGAINST

`email_ingest/blocklist.py` says in its own docstring that `no-reply@`
is deliberately NOT treated as bulk, "which tender and procurement
portals use for genuine RFQs". Then on 30 October two procurement
portals were added to the domain list -- "Procol RFX notifications
(15+)" -- and a domain entry overrides all of that reasoning.

Both readings have something in them. The mail IS machine-generated,
repetitive, and mostly notification noise: "your response is due",
"an event has been updated". And buried in that stream are the
enquiries themselves. Blocking the domain loses the second to be rid
of the first.

SO NEITHER BLOCK NOR LEAD: A LEAD THAT ANNOUNCES ITSELF AS UNREAD

A portal message becomes a lead flagged `needs_review`, owned by the
RFQ desk, with the reason "portal RFQ — needs review". The desk is
the one seat whose job is to look at an enquiry nobody has placed
yet, and that is exactly what this is: an RFQ whose buyer has to be
read out of the body because the sender is a robot.

THE BUYER IS IN THE BODY, NOT THE FROM

`no-reply@procol.in` is not the customer. The buying company is named
in the body -- as the event owner, the buyer organisation, or the
account the invitation is for -- and `buyer_from` reads it. Where it
cannot, the lead still lands, under "To review", flagged, with the
portal named: an enquiry on a review screen is recoverable and one
that was never recorded is not.

THE LIST IS DATA

`data/portal_senders.txt`, beside the denylist and re-read the same
way, so adding a portal is a data change rather than a release. The
two reported are seeded there.
"""
from __future__ import annotations

import os
import re
import threading

#: Where the list lives. Beside `blocked_senders.txt` deliberately:
#: the two files are read together and a domain in both is a portal,
#: because this file is the more specific statement.
_DEFAULT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))),
    'data', 'portal_senders.txt')

#: The ingest-log reason, which the Mail ingest screen shows and the
#: re-process script matches on.
REASON = 'portal RFQ — needs review'

#: What the lead's company says when the buyer cannot be read.
UNKNOWN_BUYER = 'To review'

_lock = threading.Lock()
_cache = set()
_cache_mtime = -1.0


def list_file():
    return (os.environ.get('LEAD_PORTAL_FILE') or _DEFAULT_FILE).strip()


def portal_domains():
    """The domains that are procurement portals, re-read on change."""
    global _cache, _cache_mtime
    path = list_file()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return set(_cache)
    with _lock:
        if mtime != _cache_mtime:
            found = set()
            try:
                with open(path, encoding='utf-8') as fh:
                    for line in fh:
                        text = line.split('#', 1)[0].strip().lower()
                        if text:
                            found.add(text.lstrip('@'))
            except OSError:
                return set(_cache)
            _cache, _cache_mtime = found, mtime
        return set(_cache)


def is_portal(address):
    """The portal domain this address belongs to, or ''.

    Matched by whole labels, so `notprocol.in` is never caught by
    `procol.in` and a subdomain like `eu.ansmtp.ariba.com` is.
    """
    text = (address or '').strip().lower()
    if '@' not in text:
        return ''
    domain = text.rsplit('@', 1)[-1].strip(' <>,;')
    if not domain:
        return ''
    for listed in portal_domains():
        if domain == listed or domain.endswith('.' + listed):
            return listed
    return ''


#: How a portal names the buyer. Ordered: the most explicit first, and
#: each is a phrase a portal actually prints rather than a guess at
#: one.
BUYER_PATTERNS = (
    (r'(?:buyer|customer|client)\s+(?:organisation|organization|company|'
     r'name)\s*[:\-]\s*(.{3,80})', 'the buyer organisation line'),
    (r'(?:event|rfx|rfq|tender|enquiry)\s+owner\s*[:\-]\s*(.{3,80})',
     'the event owner line'),
    (r'on\s+behalf\s+of\s+(.{3,80}?)(?:\s+(?:has|invites|requests)|[.,\n])',
     '"on behalf of"'),
    (r'(?:invited|invitation)\s+(?:you\s+)?(?:to|by)\s+(.{3,80}?)'
     r'(?:\s+to\s+|[.,\n])', 'the invitation line'),
    (r'(?:from|by)\s+(.{3,60}?)\s+(?:has\s+invited|invites\s+you)',
     'who invited us'),
    (r'(?:company|organisation|organization)\s*[:\-]\s*(.{3,80})',
     'the company line'),
)

_BUYER = tuple((re.compile(p, re.I), why) for p, why in BUYER_PATTERNS)

#: Words that mean the line found the portal rather than the buyer.
_NOT_A_BUYER = ('procol', 'ariba', 'sap', 'coupa', 'jaggaer', 'gem',
                'procam', 'no-reply', 'noreply', 'donotreply',
                'notification', 'support', 'admin', 'portal')

_HEAD = 3000


def buyer_from(subject='', body=''):
    """(company, how) read out of a portal message, or ('', '').

    The From is a robot, so the buyer is whatever the body says. Each
    candidate is checked against `_NOT_A_BUYER` -- a line that
    answered "Procol" or "Ariba" found the portal, which is the one
    answer worse than none because it would file every portal RFQ
    under one invented customer.
    """
    text = f"{subject or ''}\n{(body or '')[:_HEAD]}"
    for pattern, how in _BUYER:
        for found in pattern.finditer(text):
            name = _tidy(found.group(1))
            if not name:
                continue
            low = name.lower()
            if any(bad in low for bad in _NOT_A_BUYER):
                continue
            return name, how
    return '', ''


def _tidy(raw):
    """A company name out of a captured line."""
    text = ' '.join(str(raw or '').split())
    # Portals end the line with the rest of a sentence; cut at the
    # first thing that cannot be part of a name.
    text = re.split(r'\s*[\|<>\(\)\[\]]|\s{2,}|\s+-\s+', text)[0]
    text = text.strip(' .,:;-–—\'"')
    if len(text) < 3 or len(text) > 80:
        return ''
    # A line that is only a URL or an address is not a name.
    if '@' in text or text.lower().startswith(('http', 'www.')):
        return ''
    if not re.search(r'[A-Za-z]{3}', text):
        return ''
    return text


def names_the_portal(name, portal=''):
    """Is this "company" the portal itself rather than a buyer?

    14 Nov. The parser reads a company out of the sender domain, so a
    portal RFQ arrived with the company "Procol" -- not empty and not
    one of the placeholders, so the buyer read out of the body was
    never used and every portal RFQ would have been filed under one
    invented customer. That is the one answer `buyer_from` already
    refuses, and it has to be refused here too.
    """
    low = ' '.join(str(name or '').split()).lower()
    if not low:
        return False
    if any(bad in low for bad in _NOT_A_BUYER):
        return True
    for label in str(portal or '').lower().split('.'):
        if len(label) > 3 and label in low:
            return True
    return False


def describe(address, subject='', body=''):
    """What the intake records about a portal message."""
    portal = is_portal(address)
    if not portal:
        return None
    buyer, how = buyer_from(subject, body)
    event = event_key(address, subject, body)
    kind = notice_kind(subject, body)
    return {
        'portal': portal,
        'buyer': buyer,
        'how': how,
        'company': buyer or UNKNOWN_BUYER,
        #: The event this message belongs to, so the set of mails
        #: about one auction becomes one lead and not five.
        'event': event,
        'notice': kind,
        'reason': (f'{REASON} (via {portal}'
                   + (f'; {event}' if event else '')
                   + (f'; {kind}' if is_notice(kind) else '')
                   + (f'; buyer read from {how}' if buyer
                      else '; the buyer could not be read') + ')'),
    }


# ── one lead per portal EVENT, not per message ───────────────────────
#
# 13 Nov. Of the 74 messages held back by the denylist, about 30 are
# "withdrawn" notices and most of the rest are reminders. Re-processing
# them one lead per message would have made 74 leads for perhaps a
# dozen real auctions, which is a worse state than the block was.
#
# The event is the thing. A portal prints its number in every mail of
# the set -- the invitation, the reminder, "technical live", the
# corrigendum and the withdrawal all carry it -- so the number is what
# holds them together. `Lead.portal_key` already exists for exactly
# this (CRM-COCKPIT-02 §5.6, the Indo Tech reverse auctions) and this
# is the same mechanism with a wider key: the numbers the two reported
# portals use, which §5.6 did not know.
#
# THE WIDER KEY IS ONLY EVER APPLIED TO A PORTAL
#
# `quote_due.portal_key` is deliberately narrow because it runs on
# every message in the mailbox, and "RFQ-001" in two customers' own
# mail is two customers, not one event. So the broad patterns below
# are asked **only** when the sender is a listed portal, where the
# number is a machine's primary key and not a human's filing habit.

#: How the two reported portals number an event.
#: `no.`, `number`, `#` and `id` between the word and the digits are
#: all shapes these mailboxes carry -- "RFx no. 44711" is how Procol
#: writes it in a reminder and "RFX 44711" is how it writes the
#: invitation, and the two have to read as one event.
_SEP = r'[\s\-_#:]*(?:no\.?|number|id)?[\s\-_#:]*'

EVENT_PATTERNS = (
    (r'\bRFX' + _SEP + r'(\d{3,10})\b', 'RFX-{}'),
    (r'\bRFQ' + _SEP + r'(\d{3,10})\b', 'RFQ-{}'),
    (r'\bRFP' + _SEP + r'(\d{3,10})\b', 'RFP-{}'),
    # Ariba: Doc1234567890 in the subject, "Event ID" in the body.
    (r'\bDoc(?:ument)?[\s\-_#:]*(?:id[\s:#]*)?(\d{6,14})\b',
     'ARIBA-DOC{}'),
    (r'\bEvent[\s\-_#:]*id[\s:#\-]*([A-Za-z0-9\-]{3,24})\b',
     'ARIBA-EVENT{}'),
)

_EVENT = tuple((re.compile(p, re.I), shape) for p, shape in EVENT_PATTERNS)


def event_key(address='', subject='', body=''):
    """The portal event this message belongs to, or ''.

    `quote_due.portal_key` first and unchanged, so a lead already
    keyed on RA-57103 keeps that key and nothing outside the portals
    changes. The portal's own numbering is tried only for a listed
    portal -- see above.

    The subject is tried before the body for the same reason
    `portal_key` does it: a notice that quotes the mail before it
    carries that mail's number further down.
    """
    try:
        from app.services import quote_due
        found = quote_due.portal_key(subject or '', body or '')
    except Exception:                                     # noqa: BLE001
        found = ''
    if found:
        return found
    if not is_portal(address):
        return ''
    for text in (subject or '', body or ''):
        if not text:
            continue
        for pattern, shape in _EVENT:
            m = pattern.search(text)
            if m:
                return shape.format(m.group(1).upper())[:60]
    return ''


# ── what KIND of mail about the event this is ────────────────────────
#: The invitation itself: the one that is worth a lead of its own.
INVITE = 'invite'
#: "Your response is due", "you have not responded".
REMINDER = 'reminder'
#: "Technical bid live", a corrigendum, an updated event.
TECHNICAL = 'technical'
#: "About to end", "closes in two hours" — carries a deadline.
ENDING = 'ending'
#: "Withdrawn by the buyer". The event is over and we did not lose it
#: on price.
WITHDRAWN = 'withdrawn'

KINDS = (INVITE, REMINDER, TECHNICAL, ENDING, WITHDRAWN)

#: What the lead's `lost_reason` says when a buyer withdraws. Asked
#: for in these words on 13 Nov. It is not one of the six offered in
#: the Lost dialog, deliberately: an auction the buyer called off is
#: not a deal we lost, and the Lost-Reason chart should not fold it
#: into "Client dropped project".
LOST_REASON = 'withdrawn by buyer'

#: Ordered. A withdrawal of an event that was about to end is a
#: withdrawal; a reminder is the weakest reading and comes last.
NOTICE_PATTERNS = (
    (WITHDRAWN, (r'\bwithdraw(?:n|al|s|ing)?\b',
                 r'\bcancell?ed\b',
                 r'\bretracted\b',
                 r'\bevent\s+(?:has\s+been\s+)?(?:closed|discontinued|'
                 r'abandoned)\b')),
    (ENDING, (r'\babout\s+to\s+end\b',
              r'\bending\s+soon\b',
              r'\bends?\s+(?:on|at|in)\b',
              r'\bclos(?:es|ing)\s+(?:soon|on|at|in)\b',
              r'\blast\s+chance\b',
              r'\bfinal\s+call\b')),
    (TECHNICAL, (r'\btechnical(?:ly)?\s+(?:live|bid|stage|round|'
                 r'evaluation)\b',
                 r'\bcorrigendum\b',
                 r'\bamend(?:ed|ment)\b',
                 r'\b(?:event|rfx|rfq|auction)\s+(?:has\s+been\s+)?'
                 r'(?:updated|revised|extended)\b',
                 r'\bauction\s+is\s+(?:now\s+)?live\b',
                 r'\bprice\s+bid\b')),
    (REMINDER, (r'\breminder\b',
                r'\bresponse\s+is\s+due\b',
                r'\bawaiting\s+your\s+(?:response|quote|offer|bid)\b',
                r'\b(?:have\s+)?not\s+(?:yet\s+)?(?:responded|'
                r'submitted|quoted)\b',
                r'\bpending\s+(?:your\s+)?(?:response|submission|bid)\b',
                r'\bplease\s+submit\b')),
)

_NOTICE = tuple((kind, tuple(re.compile(p, re.I) for p in patterns))
                for kind, patterns in NOTICE_PATTERNS)


def notice_kind(subject='', body=''):
    """INVITE | REMINDER | TECHNICAL | ENDING | WITHDRAWN.

    The subject carries the kind on both reported portals, and the
    head of the body is read as well because Ariba puts "This event
    has been withdrawn" in the first paragraph and nothing in the
    subject but the document number.
    """
    text = f"{subject or ''}\n{(body or '')[:_HEAD]}"
    for kind, patterns in _NOTICE:
        if any(p.search(text) for p in patterns):
            return kind
    return INVITE


def is_notice(kind):
    """Anything that is not the invitation itself."""
    return kind in (REMINDER, TECHNICAL, ENDING, WITHDRAWN)
