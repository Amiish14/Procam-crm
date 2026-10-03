"""
Who the CRM is allowed to email.

The CRM talks to customers through people, not through itself. Every
address it sends to belongs to a colleague: an owner being told a lead
is theirs, a head getting the Thursday pack, an administrator getting
the health report. Nothing it sends is meant for a customer, and a
notification that reached one would be an incident — the original RFQ
is attached to some of these emails, and that is a client's document.

So the rule is a hard block, not a warning, and it lives in front of
the one transport (`email_ingest.notifier.send`) rather than at each
call site, because a rule that depends on every caller remembering it
is not a rule.

    CRM_INTERNAL_EMAIL_DOMAINS   comma-separated, added to the built-in
                                 list. Subdomains of an allowed domain
                                 are allowed.
    CRM_EMAIL_ALLOW_EXTERNAL     'true' lifts the block. It exists so a
                                 future, deliberate decision to email
                                 outside does not need a code change —
                                 not as a convenience. Leave it unset.

Everything here is pure: no database, no Flask. It is called from the
transport, from the outbox before a row is queued, and from the tests.
"""
from __future__ import annotations

import os

#: Procam's own mail domains. A new one is added here or in the
#: environment variable; both are read every call so a change to the
#: environment takes effect on the next restart without a deploy.
BUILTIN_DOMAINS = ('procamlogistics.com', 'procamgroup.in')


def _env_domains():
    raw = os.environ.get('CRM_INTERNAL_EMAIL_DOMAINS') or ''
    return tuple(d.strip().lower().lstrip('@') for d in raw.split(',')
                 if d.strip())


def internal_domains():
    """Every domain the CRM may send to, lower-cased and de-duplicated."""
    out = []
    for d in BUILTIN_DOMAINS + _env_domains():
        if d and d not in out:
            out.append(d)
    return tuple(out)


def external_allowed():
    raw = (os.environ.get('CRM_EMAIL_ALLOW_EXTERNAL') or '').strip().lower()
    return raw in ('1', 'true', 'yes', 'on')


def domain_of(address):
    addr = (address or '').strip().lower()
    # Tolerate "Name <a@b.com>" — the transport is given bare addresses
    # today, but a caller that passes a display name must not slip an
    # external address past the check by wrapping it.
    if '<' in addr and '>' in addr:
        addr = addr[addr.rfind('<') + 1:addr.rfind('>')].strip()
    if addr.count('@') != 1:
        return ''
    return addr.rsplit('@', 1)[1].strip().strip('.')


def is_internal(address):
    """True when this address is one the CRM may write to."""
    dom = domain_of(address)
    if not dom:
        return False
    for allowed in internal_domains():
        if dom == allowed or dom.endswith('.' + allowed):
            return True
    return False


def check(recipients):
    """Split a recipient list. Returns (allowed, blocked).

    `blocked` carries the addresses as given, so the Email Health screen
    can show exactly what was refused and the administrator can decide
    whether the person's address on file is wrong or the domain belongs
    on the list.
    """
    allowed, blocked = [], []
    for raw in recipients or []:
        addr = (raw or '').strip()
        if not addr:
            continue
        if external_allowed() or is_internal(addr):
            allowed.append(addr)
        else:
            blocked.append(addr)
    return allowed, blocked


def describe():
    """One line for the health screen and the runbook."""
    if external_allowed():
        return ('External email is ALLOWED — CRM_EMAIL_ALLOW_EXTERNAL is '
                'set. Every address is accepted.')
    return 'Internal only: ' + ', '.join('@' + d for d in internal_domains())
