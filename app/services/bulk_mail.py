"""Newsletters, circulars and campaign mail. Not enquiries.

CRM-FINAL-13NOV item 13: promotional mail becomes leads.

WHAT DECIDES IT, AND WHY NOT A SENDER LIST ALONE

A sender list is maintained by somebody remembering to maintain it,
and the senders change faster than anybody updates it. The mail
itself says what it is, in headers written for exactly this purpose:

    List-Unsubscribe      RFC 2369. A mail carrying it was sent by a
                          mailing system and expects to be
                          unsubscribed from. No enquiry has one.
    Precedence: bulk      RFC 2076, and the same statement in older
                          form. `list` and `junk` say it too.
    List-Id / List-Post   a mailing list, by its own declaration.
    Auto-Submitted        RFC 3834: generated, not written.
    X-Campaign / X-Mailer a campaign tool naming itself.

Those are facts about the message. The learned sender list is read on
top of them, because a few senders mail campaigns with none of these
headers -- but it is the second question, not the first.

AND THE HALF THAT MATTERS MORE: NO RFQ INTENT

A shipping line's circular and that same line's quotation arrive from
the same address, often with the same headers, because both go out
through the same marketing system. So a mail is only promotional if
it ALSO shows no sign of being an enquiry: no RFQ words, no route, no
cargo, no reference, no attachment that looks like a tender document.

Getting that the wrong way round loses business, and losing one real
RFQ costs more than a hundred newsletters on a board. The default,
when the two halves disagree, is that it is an enquiry.

WHAT HAPPENS TO ONE

It goes to Mail ingest with the reason "promotional, not a lead" --
visible, counted, with the sender and the subject, and an administrator
can turn it into a lead from that screen with one button, which is the
path for the day this is wrong. Nothing is deleted and no sender is
blocked: the address that sends a circular this week sends a real
quotation next week, which is why `junk_delete`'s `informational`
reason does not block a sender either.
"""
from __future__ import annotations

import re

#: The ingest-log reason. The Mail ingest screen shows it and the
#: archive script matches on it.
REASON = 'promotional, not a lead'

#: Headers that say "this was sent by a mailing system".
BULK_HEADERS = ('list-unsubscribe', 'list-unsubscribe-post', 'list-id',
                'list-post', 'list-help', 'x-campaign-id',
                'x-campaignid', 'x-mailchimp-id', 'x-sg-eid',
                'x-marketing-campaign', 'x-csa-complaints')

#: Headers whose VALUE decides it.
VALUED = {
    'precedence': ('bulk', 'list', 'junk'),
    'auto-submitted': ('auto-generated', 'auto-replied', 'auto-notified'),
    'x-auto-response-suppress': ('all', 'oof', 'autoreply'),
}

#: Words that make a mail an enquiry. If any of these is present the
#: mail is NOT treated as promotional, whatever its headers say.
INTENT_PATTERNS = (
    r'\brfq\b', r'\brequest for quot', r'\bquotation\b', r'\bquote\b',
    r'\btender\b', r'\bbid\b', r'\benquiry\b', r'\binquiry\b',
    r'\bkindly\s+(?:quote|offer|share\s+(?:your\s+)?(?:rate|best))',
    r'\bplease\s+(?:quote|offer|arrange|provide\s+(?:your\s+)?rate)',
    r'\bbest\s+rate', r'\byour\s+(?:lowest|best)\b',
    r'\bshipment\b.*\b(?:from|to)\b', r'\bconsignment\b',
    r'\bpick\s*up\b.*\bdeliver', r'\bex[-\s]?works\b',
    r'\bfcl\b', r'\blcl\b', r'\bodc\b', r'\bbreak\s*bulk\b',
    r'\b\d+\s*(?:mt|ton|tons|tonnes|kgs?|cbm)\b',
    r'\b(?:20|40)\s*(?:ft|feet|hq|gp)\b',
    r'\bpo\s*(?:no|number)\b', r'\bwork\s+order\b',
)

_INTENT = tuple(re.compile(p, re.I) for p in INTENT_PATTERNS)

#: Attachments that are the enquiry rather than a brochure.
DOC_HINTS = ('tender', 'rfq', 'boq', 'scope', 'annexure', 'spec',
             'enquiry', 'nit', 'corrigendum', 'bid')

#: How much body to read. A circular declares itself at the top and
#: its footer is a page of unsubscribe boilerplate.
_HEAD = 2000


def _headers(msg):
    try:
        from app.services import lead_intake as li
        return {str(k).lower(): str(v) for k, v in (li.headers(msg) or {}).items()}
    except Exception:                                     # noqa: BLE001
        return {}


def header_evidence(msg):
    """The header that says this is bulk, or ''."""
    found = _headers(msg)
    for name in BULK_HEADERS:
        if found.get(name):
            return name
    for name, values in VALUED.items():
        value = (found.get(name) or '').strip().lower()
        if value and any(v in value for v in values):
            return f'{name}: {value[:40]}'
    return ''


def known_bulk_sender(address):
    """A sender an administrator has marked as sending circulars.

    `not_relevant_senders` is the register the junk path already
    writes to, so marking one newsletter teaches this -- rather than a
    second list that has to be kept in step with the first.
    """
    text = (address or '').strip().lower()
    if not text:
        return False
    try:
        from app.services import not_relevant_senders as blocked
    except Exception:                                     # noqa: BLE001
        return False
    try:
        # `matches` answers (True, pattern, why).
        found, _pattern, _why = blocked.matches(text)
        return bool(found)
    except Exception:                                     # noqa: BLE001
        return False


def enquiry_intent(subject='', body='', filenames=()):
    """The sign that this is an enquiry after all, or ''.

    Checked whatever the headers say. A shipping line's circular and
    its quotation come from the same address through the same
    marketing system, and only one of them is business.
    """
    text = f"{subject or ''}\n{(body or '')[:_HEAD]}"
    for pattern in _INTENT:
        found = pattern.search(text)
        if found:
            return found.group(0).strip().lower()
    for name in (filenames or ()):
        low = str(name or '').lower()
        for hint in DOC_HINTS:
            if hint in low:
                return f'an attachment named {name}'
    return ''


def classify(msg, body_text='', *, sender=None):
    """(is_promotional, why). Never raises.

    Both halves have to hold: the mail declares itself bulk (or its
    sender is known for it), and nothing in it reads as an enquiry.
    """
    try:
        subject = (msg or {}).get('subject') or ''
    except Exception:                                     # noqa: BLE001
        return False, ''
    address = sender or ''
    names = ()
    try:
        from app.services import lead_intake as li
        address = address or (li.sender(msg) or '')
        names = li.attachment_names(msg) or ()
    except Exception:                                     # noqa: BLE001
        pass

    evidence = header_evidence(msg)
    if not evidence and known_bulk_sender(address):
        evidence = 'a sender already marked as sending circulars'
    if not evidence:
        return False, ''

    intent = enquiry_intent(subject, body_text, names)
    if intent:
        # The headers say bulk and the content says business. Business
        # wins: one lost RFQ costs more than a hundred newsletters.
        return False, (f'bulk ({evidence}) but it reads as an enquiry '
                       f'({intent}) — kept')
    return True, f'{REASON} ({evidence})'
