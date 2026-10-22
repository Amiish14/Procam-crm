"""
Mail from a machine is not an enquiry.

Two Microsoft 365 quarantine digests became leads called
"Microsoft", sat in the pipeline until somebody closed them, and
one of them is why the word "Claude" was in a notes field — the
digest was listing a spammer's subject line. They are not the only
ones: a bounce, an out-of-office and a delivery notification all
arrive in leads@ looking like mail from a person.

The inbox is the filter, deliberately: `single_message` captures
everything because a missed enquiry costs more than a junk lead.
This narrows that to senders that are definitionally not customers
— the postmaster, the mailer daemon, a quarantine robot — and
nothing else.

Blocked mail is kept
    It is written to `mail_ingest_log` with outcome `blocked` and a
    reason naming the pattern that caught it, so an administrator
    filtering that screen sees exactly what was held and why. No
    message is deleted, and nothing is decided silently: if the
    rule catches something it should not, the evidence is right
    there and the pattern can be turned off in Master Data.

Editable without a release
    The list is the Master Data list `system_sender`, so adding the
    next quarantine robot is an admin action. The seeds below are
    only used while that list is empty — once a person has touched
    it, their list is the list, including if they empty it.
"""
from __future__ import annotations

import re

LIST_KEY = 'system_sender'

#: Seeded on first run. Each is (pattern, why), and the why is
#: shown to the administrator rather than kept in a developer's
#: head.
SEEDS = (
    ('quarantine@messaging.microsoft.com',
     'Microsoft 365 quarantine digest — a list of mail somebody '
     'else tried to send us'),
    ('*@messaging.microsoft.com',
     'Microsoft 365 messaging robots'),
    ('*@protection.outlook.com',
     'Exchange Online Protection notices'),
    ('postmaster@*', 'delivery reports and non-delivery notices'),
    ('mailer-daemon@*', 'the mail system reporting a failure'),
    ('bounce@*', 'bounce handling'),
    ('bounces@*', 'bounce handling'),
    # Named senders, not patterns. Each one was read before it was
    # added; see the note below on why the general form is absent.
    ('googlealerts-noreply@google.com',
     'Google Alerts digests — 32 in 90 days, none an enquiry'),
    ('noreply@justdial.com', 'directory listing marketing'),
    ('no-reply@peoplemattersonline.com',
     'HR conference marketing'),
)

#: `noreply@*` is NOT here, and neither are `no-reply@*`,
#: `donotreply@*` or `do-not-reply@*`. They were, until the 90-day
#: report was read.
#:
#: Those four would have held 247 leads, and 226 of them were
#: freight load postings from SuperProcure — "Load posted by Isgec
#: Heavy Engineering Ltd", route, vehicle type, tonnage, loading
#: date — plus RFQ invitations from SAP Ariba: "Jindal Stainless
#: has invited you to participate in an event: RFQ_Transporter
#: Required". Two of the Ariba ones are open opportunities with
#: owners right now.
#:
#: A procurement portal sends from noreply@ precisely because the
#: reply goes into the portal, not the mailbox. The address says
#: nothing about whether the message is business. Blocking that
#: shape would have silently dropped the enquiries this company
#: exists to win, which is the one failure the capture-everything
#: inbox was designed to prevent.
#:
#: A robot gets named, individually, after somebody has read what
#: it sends.

REASON = 'System mail – not a lead'


def _compile(pattern):
    """`no-reply@*` and `*@domain.com` and a plain address.

    Only `*` is special, and only as a whole run of characters.
    Anything cleverer would be a small regex language in a field
    an administrator types into, and the first mistake would
    silently block a customer.
    """
    text = (pattern or '').strip().lower()
    if not text:
        return None
    escaped = '.*'.join(re.escape(p) for p in text.split('*'))
    return re.compile(r'\A' + escaped + r'\Z')


def patterns():
    """[(pattern, why)] in force. The admin list, else the seeds."""
    rows = []
    try:
        from app.models.master_data import MasterItem

        items = (MasterItem.query
                 .filter_by(list_key=LIST_KEY)
                 .order_by(MasterItem.sort_order.asc(),
                           MasterItem.id.asc()).all())
        if items:
            # Touched by a person: their list is the list, and an
            # item they deactivated stays off rather than being
            # helpfully restored from the seeds.
            return [(i.code, i.description or i.label)
                    for i in items if i.is_active]
    except Exception:                                     # noqa: BLE001
        # No table yet, or no application context — a script
        # importing this module. The seeds answer.
        pass
    return list(SEEDS)


def blocks(address, *, rules=None):
    """(True, pattern, why) when this sender is a machine."""
    sender = (address or '').strip().lower()
    if not sender:
        return (False, '', '')
    for pattern, why in (rules if rules is not None else patterns()):
        rx = _compile(pattern)
        if rx is not None and rx.match(sender):
            return (True, pattern, why)
    return (False, '', '')


def enabled():
    """Off until somebody turns it on.

    The report is run first and read: a rule that blocks mail is
    not a rule to switch on from a dry run nobody looked at.
    """
    from app.services import flags
    try:
        return bool(flags.on('FEATURE_SENDER_BLOCK'))
    except Exception:                                     # noqa: BLE001
        return False


def seed(actor='system'):
    """Put the starting list in Master Data. Caller commits."""
    from app import db
    from app.models.master_data import MasterItem

    made = []
    for order, (pattern, why) in enumerate(SEEDS):
        exists = MasterItem.query.filter_by(
            list_key=LIST_KEY, code=pattern).first()
        if exists is not None:
            continue
        db.session.add(MasterItem(
            list_key=LIST_KEY, code=pattern, label=pattern,
            description=why, sort_order=order, is_active=True,
            created_by=actor))
        made.append(pattern)
    return made
