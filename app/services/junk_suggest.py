"""
"Possible junk" — a chip, not a decision.

§9 of CRM-JUNK-DELETE-01. This never deletes anything and never
hides anything. It puts a word next to a lead so that the person
whose board it is can look at it, and the only thing that follows
is them clicking Delete or ignoring the chip.

That restraint is the whole design. The sender block list earns
its keep by being narrow — `noreply@*` was left out of it because
226 of the 247 leads it would have held were freight load postings
and SAP Ariba RFQ invitations, two of which are open opportunities
with owners right now. A suggestion can afford to be wider than a
block precisely because it costs nothing to be wrong: the worst
case is a chip somebody ignores.

Off until FEATURE_JUNK_SUGGEST is switched on.
"""
from __future__ import annotations

import re

#: Mailbox providers people use personally. A customer may well mail
#: from one — several do — so this is never a reason on its own; it
#: counts only when the lead also has no company worth the name.
FREE_MAIL = (
    'gmail.com', 'yahoo.com', 'yahoo.co.in', 'hotmail.com',
    'outlook.com', 'rediffmail.com', 'live.com', 'icloud.com',
    'protonmail.com', 'ymail.com',
)

#: What the ingest writes into `company` when it could not find one.
NO_COMPANY = ('', 'unknown', 'n/a', 'na', '-', 'none')

#: Headers only bulk mail carries. Read from the stored raw headers
#: where the ingest kept them.
BULK_HEADERS = (
    'list-unsubscribe', 'list-id', 'precedence: bulk',
    'x-campaign', 'x-mailer: mailchimp', 'feedback-id',
)

#: Words that are a newsletter's whole subject line.
BULK_SUBJECT = re.compile(
    r'\b(newsletter|unsubscribe|webinar|promotion|offer ends|'
    r'sale ends|discount|free trial|subscribe now|digest)\b',
    re.I)


def enabled():
    from app.services import flags
    try:
        return bool(flags.on('FEATURE_JUNK_SUGGEST'))
    except Exception:                                     # noqa: BLE001
        return False


def _domain(address):
    address = (address or '').strip().lower()
    return address.rsplit('@', 1)[-1] if '@' in address else ''


def _staff_addresses():
    """Personal addresses colleagues are on file with.

    A mail from a colleague's own gmail to leads@ is the case that
    started this ticket. The employee master is the list, so nobody
    has to be named in code.
    """
    try:
        from app import Employee

        out = set()
        for emp in Employee.query.all():
            for field in ('personal_email', 'alt_email', 'email'):
                value = (getattr(emp, field, '') or '').strip().lower()
                if value and _domain(value) in FREE_MAIL:
                    out.add(value)
        return out
    except Exception:                                     # noqa: BLE001
        return set()


def reasons(lead, *, staff=None):
    """[sentence] — why this might not be an enquiry. Empty means no
    opinion, which is the usual answer."""
    out = []
    from app.services import junk_delete as jd

    sender = jd.sender_of(lead)
    domain = _domain(sender)

    try:
        from app.services import system_senders
        hit, pattern, _why = system_senders.blocks(sender)
        if hit:
            out.append(f'the sender matches the block list ({pattern})')
    except Exception:                                     # noqa: BLE001
        pass

    staff = _staff_addresses() if staff is None else staff
    if sender and sender in staff:
        out.append('it came from a colleague’s personal address')
    elif domain and domain in FREE_MAIL:
        company = (lead.company or '').strip().lower()
        if company in NO_COMPANY:
            out.append('a personal mailbox with no company name')

    headers = (getattr(lead, 'original_email_body', '') or '')[:4000]
    raw = headers.lower()
    for header in BULK_HEADERS:
        if header in raw:
            out.append(f'the mail carries a {header} header')
            break

    subject = getattr(lead, 'original_email_subject', '') or ''
    if BULK_SUBJECT.search(subject):
        out.append('the subject reads as a mailing, not an enquiry')

    return out


def chip(lead, *, staff=None):
    """(show, tooltip) for the board. Never shown on a worked lead."""
    if not enabled():
        return (False, '')
    from app.services import junk_delete as jd

    if lead.junk_deleted or (lead.stage or '') != jd.ELIGIBLE_STAGE:
        return (False, '')
    why = reasons(lead, staff=staff)
    if not why:
        return (False, '')
    return (True, 'Possible junk — ' + '; '.join(why)
                  + '. Nobody has decided; open it and see.')
