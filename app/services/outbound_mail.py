"""
Our own mail, going out — whose enquiry does it belong to?

CRM-ACCOUNT-FIRST-01.

What went wrong
    On 1–3 September, 167 messages a Procam employee had sent *to*
    a customer were ingested as 167 new leads, all filed under the
    placeholder account "Unknown" and all assigned to one person.
    They are real RFQ and quotation threads with Alstom, GE
    Vernova, MSC, Nippon Express and others, and the CRM holds
    them as orphans.

    The cause is simple. Every matching rule reads the **sender**,
    and on our own outbound mail the sender is us. So the customer
    was invisible, even though their address was sitting in the
    `To:` line of the same message.

What this does
    Reads the recipient instead. An internal sender plus at least
    one external recipient is outbound; the first external
    recipient names the customer; and the customer's open lead is
    where the message belongs. A new lead is created only when
    that customer has no open lead at all — and then it is created
    *with* the account on it, which is the whole point.

Where it looks
    The envelope first, and then the forwarded header block in the
    body. The 167 carry nothing in `original_email_from`; their
    sender exists only as a "From: … To: …" block inside the text,
    so an envelope-only reading finds nothing and the bug repeats.

One rule it will not break
    Our own domain is never the customer. An account that claims
    one of ours is refused outright — #983 "Unknown" listed
    `procamgroup.in` and would otherwise have matched every one of
    these messages exactly.
"""
from __future__ import annotations

import re

#: The forwarded header Outlook writes at the top of a quoted
#: message. Both orders occur; `Sent:` and `Date:` both appear.
RX_FROM = re.compile(r'^\s*From:\s*(.+)$', re.M)
RX_TO = re.compile(r'^\s*To:(.*?)(?=^\s*(?:Cc|Bcc|Subject|Sent|Date):|\Z)',
                   re.S | re.M)
RX_SUBJECT = re.compile(r'^\s*Subject:\s*(.+)$', re.M)
RX_ADDR = re.compile(r'[\w.+%-]+@[\w.-]+\.\w{2,}')


def _addrs(text):
    return [a.strip().lower() for a in RX_ADDR.findall(text or '')]


def is_internal(address):
    from app.services.mail_signals import is_internal as _ours
    return _ours(address)


def read_header(body):
    """(sender, [recipients], subject) from a forwarded header block.

    Only the first block. A long thread repeats the header once per
    reply, and the one at the top is the message this actually is.
    """
    text = (body or '')[:6000]
    sender = ''
    hit = RX_FROM.search(text)
    if hit:
        found = _addrs(hit.group(1))
        sender = found[0] if found else ''
    to = []
    hit = RX_TO.search(text)
    if hit:
        to = _addrs(hit.group(1))
    subject = ''
    hit = RX_SUBJECT.search(text)
    if hit:
        subject = hit.group(1).strip()
    return sender, to, subject


def direction(msg=None, body='', *, sender=None, to=None):
    """('outbound'|'', sender, [external recipients]).

    Outbound means one of ours wrote to somebody outside. Mail
    between two Procam addresses is internal and is not a lead at
    all; mail from outside is the ordinary inbound case other rules
    already handle.
    """
    from app.services import lead_intake as li

    if sender is None and msg is not None:
        sender = li.sender(msg)
    if to is None and msg is not None:
        to = (li.recipients(msg, 'toRecipients')
              + li.recipients(msg, 'ccRecipients'))
    sender, to = (sender or ''), list(to or [])

    if not sender or not is_internal(sender):
        # Nothing in the envelope says this is ours. Try the
        # forwarded header, which is all the 167 ever had.
        h_sender, h_to, _subject = read_header(body)
        if h_sender and is_internal(h_sender):
            sender, to = h_sender, h_to
        else:
            return '', sender, []

    external = [a for a in to if a and not is_internal(a)]
    if not external:
        return '', sender, []
    return 'outbound', sender, external


def customer_of(external, *, count_hit=False):
    """(account, rule, address) for the first recipient we can place.

    Never our own domain, and never a placeholder account: those
    are how 4,435 leads ended up under "Unknown".
    """
    from app.services import account_match

    for address in external:
        account, rule = account_match.resolve(address,
                                              count_hit=count_hit)
        if account is None:
            continue
        if account_match.not_a_customer(account):
            continue
        return account, rule, address
    return None, '', (external[0] if external else '')


def open_lead_for(account, *, subject='', within_days=180):
    """The open lead this outbound message belongs on, or None.

    By account first, because the customer is the thing we are sure
    of. Among that customer's open leads, one whose subject matches
    wins; failing that the most recently touched, which is what a
    person would pick when filing a reply by hand.
    """
    from datetime import datetime, timedelta

    from app import Lead
    from app.services import sales_rules

    if account is None:
        return None
    since = datetime.utcnow() - timedelta(days=within_days)
    rows = (Lead.query
            .filter(Lead.company_id == account.id,
                    Lead.is_archived.isnot(True),
                    Lead.stage.in_(sales_rules.open_stages()),
                    Lead.created_at >= since)
            .order_by(Lead.updated_at.desc()).all())
    if not rows:
        return None

    want = strip_subject(subject)
    if want:
        for lead in rows:
            for raw in (lead.original_email_subject,
                        lead.forwarded_subject):
                if strip_subject(raw) == want:
                    return lead
    return rows[0]


def strip_subject(subject):
    """"Re: Fw: [EXTERNAL] X" -> "x". Empty when too generic."""
    s = (subject or '').strip()
    for _pass in range(6):
        s = re.sub(r'^\s*(re|fw|fwd|rf|aw|tr)\s*:\s*', '', s,
                   flags=re.I)
        s = re.sub(r'^\s*\[[^\]]{1,20}\]\s*', '', s)
    s = re.sub(r'\s+', ' ', s).strip().lower()
    return s if len(s) >= 6 else ''
