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


def describe(address, subject='', body=''):
    """What the intake records about a portal message."""
    portal = is_portal(address)
    if not portal:
        return None
    buyer, how = buyer_from(subject, body)
    return {
        'portal': portal,
        'buyer': buyer,
        'how': how,
        'company': buyer or UNKNOWN_BUYER,
        'reason': (f'{REASON} (via {portal}'
                   + (f'; buyer read from {how}' if buyer
                      else '; the buyer could not be read') + ')'),
    }
