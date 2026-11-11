"""Which half of the lead's correspondence a message belongs to.

CRM-MAIL-2026-11-11 §11: on three leads the customer's own RFQ was
showing under "Rate sourcing — what we asked suppliers, kept out of
the customer thread".

TWO FAULTS, AND THEY LOOK IDENTICAL ON SCREEN

The first is a display one and accounts for every lead whose trail
has no rows yet: `/api/leads/<id>/emails` falls back to the
`original_email_*` columns when it finds nothing, and the fallback
ran for every `kind`. The enquiry that started the lead was therefore
appended to the supplier list as well as the customer one. Fixed
where it happens, in the route.

The second is a filing one and needs this module. `intake_class` is
written once, from the classifier's verdict, and step 6 says "rate
sourcing" for anything whose sender domain is in the Vendor Master.
A company that supplies us and also buys from us — a filter
manufacturer, an infrastructure contractor, a forwarder — is both,
and their enquiry is then filed as our own rate request.

THE TEST IS EVIDENCE, NOT THE LABEL

So the screen does not ask what the message was called. It asks who
it is between:

    * anything we sent is supplier-facing only if it did not go to
      the customer;
    * anything that arrived from the lead's own customer — the same
      address, or the same domain, as the lead holds — is customer
      mail, whatever class it carries.

That second rule is the whole fix. It needs no backfill to take
effect, it cannot be undone by a future misclassification, and it is
the one thing we know for certain about a lead: who the customer is.

The backfill exists anyway (scripts/2026_11_11_misfiled_rate_sourcing
.py) because the stored class is read by the data-quality checks and
by the reports, not only by this screen.
"""
from __future__ import annotations

#: The class the Rate sourcing panel is built on.
RATE_SOURCING = 'G_rate_sourcing'


def _domain(address):
    text = (address or '').strip().lower()
    if '@' not in text:
        return ''
    return text.rsplit('@', 1)[-1].strip(' <>,;')


def _addresses(raw):
    """Every address in a From/To/Cc string."""
    out = []
    for part in str(raw or '').replace(';', ',').split(','):
        part = part.strip()
        if '@' in part:
            out.append(part.strip('<> '))
    return out


def customer_marks(lead):
    """(addresses, domains) that identify this lead's own customer."""
    addresses, domains = set(), set()
    for attr in ('email', 'original_email_from', 'email2'):
        value = (getattr(lead, attr, '') or '').strip().lower()
        if '@' in value:
            addresses.add(value)
            domain = _domain(value)
            if domain:
                domains.add(domain)
    try:
        from app.services import mail_policy
        domains = {d for d in domains
                   if d not in mail_policy.BUILTIN_DOMAINS}
        addresses = {a for a in addresses
                     if _domain(a) not in mail_policy.BUILTIN_DOMAINS}
    except Exception:                                     # noqa: BLE001
        pass
    try:
        from app.services import account_match
        domains = {d for d in domains
                   if not account_match.is_free_mail(d)}
    except Exception:                                     # noqa: BLE001
        pass
    return addresses, domains


def from_the_customer(lead, row):
    """Did this message arrive from the lead's own customer?

    Address first, then domain. A free-mail domain is not a mark —
    two customers on gmail.com are not one customer — and our own
    domains are not either.
    """
    if lead is None or row is None:
        return False
    if (getattr(row, 'direction', '') or '') == 'outbound':
        return False
    sender = (getattr(row, 'from_addr', '') or '').strip().lower()
    if not sender:
        return False
    addresses, domains = customer_marks(lead)
    if sender in addresses:
        return True
    return _domain(sender) in domains if domains else False


def is_supplier_facing(lead, row):
    """Whether this row belongs in the Rate sourcing panel.

    The stored class proposes; the evidence disposes.
    """
    if (getattr(row, 'intake_class', '') or '') != RATE_SOURCING:
        return False
    return not from_the_customer(lead, row)


def misfiled(lead, row):
    """A row called rate sourcing that is plainly the customer's."""
    return ((getattr(row, 'intake_class', '') or '') == RATE_SOURCING
            and from_the_customer(lead, row))
