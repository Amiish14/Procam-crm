"""Senders whose mail is never an enquiry, as a list a person can edit.

PROCAM-DECISIONS-02NOV, 2 Nov: fifteen named senders go to the
Not-relevant queue, "and add their sender domains to the Not-relevant
classes so future mail goes there".

WHY THIS IS NOT THE INGEST BLOCK LIST

`system_senders` already exists and is read first by
`accounts_module2.not_relevant_class`. It is the wrong list for this,
and the difference matters:

    system_senders        the mail never becomes a lead at all. It is
                          written to `mail_ingest_log` as blocked and
                          nobody sees it on a board.
    this list             the mail still becomes a lead, and the lead
                          classifies as not relevant.

"Future mail goes there" means the Not-relevant queue, not the ingest
log. A directory portal that sends one real enquiry a year — and some
do, because a client's buyer sometimes mails from a portal account —
would be invisible if it were blocked at ingest, and is merely filed
in the right queue if it is here.

A PATTERN, AND WHAT IT MAY NOT BE

Stored as a glob against the sender address, the same shape
`system_senders` uses, so one reader can be written for both. A
pattern that would match a Procam domain is refused: blocking
ourselves would file every internal forward as not relevant, and a
forward is how most real enquiries reach the CRM.

A bare domain is also refused. A domain on its own matches nothing,
because a sender address is `somebody@that-domain` — so the script that
writes these turns an observed domain into `*@domain` rather than
leaving a rule that silently never fires.

EDITABLE WITHOUT A RELEASE, AND SEEDED WITH NOTHING

The list is the Master Data list below. There are no seeds: a sender in
code is a sender nobody can take off, and the fifteen the decision
names are written in by
`scripts/2026_11_02_not_relevant_senders.py --apply`, from the domains
their leads actually arrived from rather than from a domain guessed
out of a company name.
"""
from __future__ import annotations

import re

LIST_KEY = 'not_relevant_sender'

#: Cached compiled patterns, keyed by the pattern text.
_CACHE = {}


def _compile(pattern):
    text = (pattern or '').strip().lower()
    if not text:
        return None
    if text not in _CACHE:
        try:
            _CACHE[text] = re.compile(
                '^' + re.escape(text).replace(r'\*', '.*') + '$')
        except re.error:
            _CACHE[text] = None
    return _CACHE[text]


def patterns():
    """[(pattern, why)] in force. Empty until somebody adds one."""
    try:
        from app.models.master_data import MasterItem
        rows = (MasterItem.query
                .filter_by(list_key=LIST_KEY)
                .order_by(MasterItem.sort_order.asc(),
                          MasterItem.id.asc()).all())
        return [(r.code, r.description or r.label)
                for r in rows if r.is_active]
    except Exception:                                     # noqa: BLE001
        # No table yet, or no application context — a script importing
        # this module before it has one.
        return []


def matches(address, *, rules=None):
    """(True, pattern, why) when this sender is on the list."""
    sender = (address or '').strip().lower()
    if not sender:
        return (False, '', '')
    for pattern, why in (rules if rules is not None else patterns()):
        rx = _compile(pattern)
        if rx is not None and rx.match(sender):
            return (True, pattern, why)
    return (False, '', '')


def why_not_a_pattern(pattern):
    """'' if this may be added, else why not."""
    text = (pattern or '').strip().lower()
    if not text:
        return 'there is no pattern'
    if '@' not in text:
        return (f'{text!r} has no @ in it, so it can never match a '
                f'sender address — write it as *@{text}')
    if text in ('*@*', '*', '*@'):
        return 'that would match every sender there has ever been'
    domain = text.rsplit('@', 1)[-1].lstrip('*.')
    if not domain or domain == '*':
        return (f'{text!r} matches every domain — name the one you mean')
    try:
        from app.services.mail_signals import OUR_DOMAINS
        if any(domain == d or domain.endswith('.' + d)
               for d in OUR_DOMAINS):
            return (f'{domain} is our own — every internal forward would '
                    f'be filed as not relevant, and a forward is how '
                    f'most real enquiries reach the CRM')
    except Exception:                                     # noqa: BLE001
        pass
    try:
        from app.services.junk_suggest import FREE_MAIL
        if domain in FREE_MAIL:
            return (f'{domain} is a personal mailbox provider — several '
                    f'real customers mail from one')
    except Exception:                                     # noqa: BLE001
        pass
    return ''


def add(pattern, *, why, actor):
    """Put one pattern on the list. Returns 'added', 'already' or a refusal."""
    from app import db
    from app.models.master_data import MasterItem

    refused = why_not_a_pattern(pattern)
    if refused:
        return refused
    text = pattern.strip().lower()
    existing = MasterItem.query.filter_by(list_key=LIST_KEY,
                                          code=text).first()
    if existing is not None:
        if not existing.is_active:
            existing.is_active = True
            return 'added'
        return 'already'
    db.session.add(MasterItem(list_key=LIST_KEY, code=text, label=text,
                              description=why, is_active=True,
                              created_by=actor))
    _CACHE.pop(text, None)
    return 'added'
