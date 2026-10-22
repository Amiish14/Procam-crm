"""
An auction is not an enquiry you can leave until Thursday.

CRM-FORWARD-01. Two things that belong together because both are
read off the same message at the same moment.

Who forwarded it
    The parser unwraps a forward and records the original sender,
    which is right: the enquiry belongs to the customer, not to
    the colleague who passed it on. What went missing with it was
    the colleague. 226 SuperProcure load postings were forwarded
    into leads@ by somebody who stopped on 11 September, and
    nothing in the CRM could say who to ask.

What kind of enquiry it is
    A spot load closes in hours and a reverse auction closes at a
    stated minute. Treating those like any other lead is how 226
    of them ended up deleted in a bulk review of "unassigned
    leads" — the reviewer had no way to tell a load posting from a
    newsletter, because nothing on the record said.

    So they are marked, and the mark is what the junk sweep and
    the bulk close both check.

The patterns are Master Data
    `auction_pattern`, admin-editable, seeded below. Each carries
    the platform it identifies in `meta`, so a new portal is a row
    rather than a release. No owner's name appears in this file:
    the owner is an admin setting read at assignment time.
"""
from __future__ import annotations

import re

LIST_KEY = 'auction_pattern'
LEAD_TYPE = 'auction'
OWNER_SETTING = 'auction_lead_owner'

#: (pattern, platform, why). The pattern is matched against the
#: subject and the sender, case-insensitively, as a substring —
#: not a regex, because an administrator edits these.
SEEDS = (
    ('load post notification', 'superprocure',
     'SuperProcure load posting'),
    ('auction start notification', 'superprocure',
     'SuperProcure auction opening'),
    ('live load post', 'superprocure',
     'SuperProcure live load'),
    ('load has been edited', 'superprocure',
     'a posting the shipper changed'),
    ('superprocure', 'superprocure', 'anything from the portal'),
    ('reverse auction', '', 'a reverse auction, any platform'),
    ('auction is now live', '', 'an auction opening'),
    ('auction has been withdrawn', '', 'an auction cancelled'),
    ('invited you to participate in an event', 'ariba',
     'SAP Ariba sourcing event'),
    ('ariba', 'ariba', 'anything from Ariba'),
)

#: "RA-1724", "RA 1738" — a reverse-auction reference. Its own
#: rule because it is a shape, not a phrase.
RX_RA = re.compile(r'\bRA[\s-]?\d{2,6}\b', re.I)

BID, NO_BID = 'bid', 'not_bid'
OUTCOMES = (BID, NO_BID)


def patterns():
    """[(pattern, platform, why)] in force — admin list, else seeds."""
    try:
        from app.models.master_data import MasterItem

        items = (MasterItem.query
                 .filter_by(list_key=LIST_KEY)
                 .order_by(MasterItem.sort_order.asc(),
                           MasterItem.id.asc()).all())
        if items:
            return [(i.code,
                     (i.meta or {}).get('platform', ''),
                     i.description or i.label)
                    for i in items if i.is_active]
    except Exception:                                     # noqa: BLE001
        pass
    return list(SEEDS)


def classify(subject='', sender='', body='', *, rules=None):
    """(lead_type, platform, why) or (None, '', '') for ordinary mail.

    Subject and sender only, plus the RA-reference shape. Not the
    body: a quotation that happens to mention "reverse auction" in
    a paragraph is not an auction notification, and a rule that
    reads the whole body would mark it as one.
    """
    haystack = f'{subject or ""} {sender or ""}'.lower()
    for pattern, platform, why in (rules if rules is not None
                                   else patterns()):
        needle = (pattern or '').strip().lower()
        if needle and needle in haystack:
            return (LEAD_TYPE, platform or _platform_from(sender),
                    why)
    if RX_RA.search(subject or ''):
        return (LEAD_TYPE, _platform_from(sender),
                'carries a reverse-auction reference')
    return (None, '', '')


def _platform_from(sender):
    host = (sender or '').rsplit('@', 1)[-1].lower()
    for known in ('superprocure', 'ariba', 'tenderdetail',
                  'procol', 'zycus', 'jaggaer'):
        if known in host:
            return known
    return ''


def is_auction(lead):
    return (getattr(lead, 'lead_type', None) or '') == LEAD_TYPE


def owner():
    """Who gets these. An admin setting, never a name in code.

    Returns '' when nobody has been nominated, and the caller
    leaves the lead for the ordinary assignment rules rather than
    inventing an owner.
    """
    try:
        from app.services import admin_settings
        return (admin_settings.get(OWNER_SETTING) or '').strip()
    except Exception:                                     # noqa: BLE001
        return ''


def seed(actor='system'):
    """Put the starting patterns in Master Data. Caller commits."""
    from app import db
    from app.models.master_data import MasterItem

    made = []
    for order, (pattern, platform, why) in enumerate(SEEDS):
        if MasterItem.query.filter_by(list_key=LIST_KEY,
                                      code=pattern).first():
            continue
        db.session.add(MasterItem(
            list_key=LIST_KEY, code=pattern, label=pattern,
            description=why, sort_order=order, is_active=True,
            meta={'platform': platform}, created_by=actor))
        made.append(pattern)
    return made


# ── who forwarded it ─────────────────────────────────────────────────
def forwarder(msg, body_text):
    """(email, name, raw_subject) when this message was forwarded on.

    The envelope sender of a forward is the colleague; the sender
    inside the body is the customer. The parser already keeps the
    second. This keeps the first, and only when the message really
    is a forward — otherwise every ordinary enquiry would record
    its own sender as having forwarded it.
    """
    from email_ingest import parser as email_parser

    subject = (msg.get('subject') or '') if isinstance(msg, dict) else ''
    try:
        split = email_parser.split_forwarded_body(body_text or '')
        is_forward = bool(split.get('is_forward'))
    except Exception:                                     # noqa: BLE001
        is_forward = False
    if not is_forward:
        is_forward = bool(
            email_parser._FORWARD_SUBJECT_RE.match(subject or ''))
    if not is_forward:
        return ('', '', '')

    who = (((msg.get('from') or {}).get('emailAddress') or {})
           if isinstance(msg, dict) else {})
    return ((who.get('address') or '').strip().lower()[:320],
            (who.get('name') or '').strip()[:160],
            (subject or '')[:500])
