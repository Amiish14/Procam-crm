"""
A vendor never learns who the client is.

FF-COMMS-01 R3. When Procam asks four agents to price a leg, each of
them must see the cargo, the route and the dates — and must not see
the customer, the consignee, or anything that identifies either. Two
agents comparing notes should not be able to work out whose job they
are bidding on, because that is how a client gets approached behind
us and how a rate gets padded once the agent knows the end user can
pay.

Two gates, not one
    `redact()` removes what it knows about before the text is built.
    `assert_clean()` reads the finished message and refuses to send
    it if anything identifying survived. The first is the feature;
    the second is the one that matters, because the first will miss
    something eventually — a name in an attachment filename, a
    client's phrasing quoted back in a scope note — and silence is
    not an acceptable failure for this.

What counts as identifying
    The account name and its aliases, the consignee, any email
    domain on the account, and any address at those domains. Short
    tokens are excluded: a client called "SP" would otherwise redact
    every occurrence of those two letters in a scope description and
    produce a message nobody can read, which is its own kind of
    failure.
"""
from __future__ import annotations

import re

from app import db

#: Below this a token is too short to redact safely — the false
#: positives destroy the message. Four characters: "Tata" survives,
#: "SP" does not, and a two-letter client needs an alias set by hand.
MIN_TOKEN = 4

#: Corporate noise. Stripped when working out what is distinctive
#: about a name, so "Tata Steel Limited" and "Tata Steel Ltd" are
#: recognised as the same client.
SUFFIXES = {
    'ltd', 'limited', 'pvt', 'private', 'plc', 'inc', 'incorporated',
    'llc', 'llp', 'gmbh', 'bv', 'nv', 'sa', 'ag', 'pte', 'corp',
    'corporation', 'company', 'holdings', 'group', 'india', 'enterprises',
    'industries', 'international', 'and', 'the',
}

#: Words that name cargo or trade rather than a client. A vendor has
#: to be told the cargo is steel in order to quote it, so these are
#: never redacted on their own — only as part of a longer run that
#: does identify somebody, such as "Tata Steel".
#:
#: This list applies to *tokens inside* a longer name, never to a
#: whole one: an account actually called "Steel" still has that word
#: redacted, because the full name is always a variant. The cost
#: there is the reverse — "steel coils" in a cargo description is
#: masked too, and the request reads badly. Leaking the client is
#: worse than reading badly, so that is the trade taken; a one-word
#: generic client is a case to rephrase the scope note around.
#:
#: The names the automatic pass genuinely cannot protect are the
#: short ones — under MIN_TOKEN characters. Production has four:
#: Acs, Efl, Qd and Uht. Those need an alias entered by hand.
GENERIC = {
    'steel', 'cement', 'power', 'energy', 'motors', 'auto', 'logistics',
    'shipping', 'cargo', 'freight', 'transport', 'chemicals', 'metals',
    'textiles', 'foods', 'pharma', 'solar', 'wind', 'heavy', 'engineering',
    'projects', 'infra', 'infrastructure', 'technologies', 'systems',
    'global', 'trading', 'exports', 'imports', 'paper', 'glass',
}

#: What a redacted mention becomes. Deliberately not blank: a reader
#: should see that something was removed rather than meet a sentence
#: with a hole in it.
MASK = '[client withheld]'
MASK_DOMAIN = '[domain withheld]'


class LeakError(RuntimeError):
    """Identifying detail survived into something a vendor would see."""


#: A phone number has to match however it was typed. +91 98765 43210,
#: 098765-43210 and 9876543210 are one number, and a vendor only needs
#: one of the three to ring the client directly.
MIN_PHONE_DIGITS = 8
MASK_PHONE = '[contact withheld]'


def phone_digits(value):
    """The dialable digits of a number, country code dropped.

    Compared on the last ten, because +91 98765 43210 and 98765 43210
    reach the same desk and a comparison that treats them as
    different protects nobody.
    """
    digits = re.sub(r'\D', '', value or '')
    if len(digits) < MIN_PHONE_DIGITS:
        return ''
    return digits[-10:] if len(digits) > 10 else digits


def _phone_pattern(digits):
    """Match those digits however they are spaced or punctuated."""
    return r'\D{0,2}'.join(re.escape(d) for d in digits)


def redact_phones(text, phones):
    if not text or not phones:
        return text
    out = str(text)
    for digits in sorted(phones, key=len, reverse=True):
        out = re.sub(_phone_pattern(digits), MASK_PHONE, out)
    return out


def find_phone_leaks(text, phones):
    return [d for d in phones
            if re.search(_phone_pattern(d), text or '')]


def contacts_of(account=None, lead=None):
    """The client's people, from the contacts table.

    A vendor who learns the buyer's name and mobile can go round us,
    and that is the whole reason this module exists — the company
    name is only the most obvious way to give it away.
    """
    from app import Contact

    ids = [i for i in ((getattr(account, 'id', None)),
                       (getattr(lead, 'company_id', None))) if i]
    if not ids:
        return []
    return Contact.query.filter(
        db.or_(Contact.company_id.in_(ids),
               Contact.account_id.in_(ids))).all()


def _tokens(name):
    return [t for t in re.split(r'[^0-9A-Za-z]+', name or '') if t]


def variants(name):
    """Every form of a client name worth looking for.

    The stored name is rarely the one a colleague typed into a scope
    note: an account called "Tata Steel Limited" turns up as "Tata
    Steel", "Tata Steel Ltd" and "TATA STEEL". Matching only the
    stored string is how a name walks straight through a redaction
    that looked like it worked — which it did here, the first time
    this was written.
    """
    out = set()
    name = (name or '').strip()
    if len(name) >= MIN_TOKEN:
        out.add(name)
    toks = _tokens(name)
    distinctive = [t for t in toks
                   if len(t) >= MIN_TOKEN and t.lower() not in SUFFIXES]
    # Runs of adjacent distinctive words: "Tata Steel" out of
    # "Tata Steel Limited".
    for i in range(len(distinctive)):
        for j in range(i + 1, len(distinctive) + 1):
            run = distinctive[i:j]
            if len(run) >= 2:
                out.add(' '.join(run))
    # Single words, but only the ones that identify somebody. "Steel"
    # alone is cargo; "Tata" alone is a client.
    for t in distinctive:
        if t.lower() not in GENERIC:
            out.add(t)
    return {v for v in out if len(v) >= MIN_TOKEN}


def _alias_set(account=None, lead=None, extra=None):
    """Everything that names this client, long enough to redact."""
    out = set()

    def add(value):
        out.update(variants(value))

    if account is not None:
        add(getattr(account, 'name', None))
        for a in (getattr(account, 'aliases', None) or []):
            add(a)
    if lead is not None:
        add(getattr(lead, 'company', None))
        add(getattr(lead, 'pic', None))
    for e in (extra or []):
        add(e)
    return out


def _domain_set(account=None, lead=None, extra=None):
    out = set()
    for d in (getattr(account, 'email_domains', None) or []):
        d = (d or '').strip().lstrip('@').lower()
        if d:
            out.add(d)
    for holder in (lead, account):
        email = (getattr(holder, 'email', None) or '').strip().lower()
        if '@' in email:
            out.add(email.split('@', 1)[1])
    for d in (extra or []):
        d = (d or '').strip().lstrip('@').lower()
        if d:
            out.add(d)
    # Our own domains are not the client's and must survive: a vendor
    # has to be able to reply to us.
    return {d for d in out
            if d not in ('procamgroup.in', 'procamlogistics.com')}


def secrets_for(*, account=None, lead=None, consignee=None,
                extra_names=None, extra_domains=None, keep=None,
                contacts=None, include_contacts=True):
    """(names, domains) that must not reach a vendor.

    `keep` is the route — origin, destination, ports. A consignee
    called "Hamburg Stahlwerke" would otherwise make *Hamburg* a
    secret, and a rate request that will not say where the cargo is
    going cannot be answered. The full phrase is still redacted; only
    the bare place name survives, which is what the vendor is being
    asked to price.
    """
    people = list(contacts if contacts is not None
                  else (contacts_of(account=account, lead=lead)
                        if include_contacts else []))

    extra = list(extra_names or []) + ([consignee] if consignee else [])
    extra += [c.name for c in people if getattr(c, 'name', None)]

    names = _alias_set(account=account, lead=lead, extra=extra)
    domains = _domain_set(account=account, lead=lead,
                          extra=list(extra_domains or [])
                          + [(c.email or '').split('@')[-1]
                             for c in people
                             if '@' in (c.email or '')])

    phones = set()
    for c in people:
        for raw in (getattr(c, 'phone', None),
                    getattr(c, 'mobile', None)):
            d = phone_digits(raw)
            if d:
                phones.add(d)
    for holder in (lead, account):
        d = phone_digits(getattr(holder, 'phone', None))
        if d:
            phones.add(d)

    protected = set()
    for value in (keep or []):
        for t in _tokens(value):
            if len(t) >= MIN_TOKEN:
                protected.add(t.lower())
    if protected:
        # Only single words are released. "Hamburg Stahlwerke" is
        # still a secret; "Hamburg" on its own is a port.
        names = {n for n in names
                 if ' ' in n or n.lower() not in protected}
    return Secrets(names, domains, phones)


class Secrets(tuple):
    """(names, domains, phones), and still a 2-tuple to unpack.

    Older callers wrote `names, domains = secrets_for(...)` and keep
    working; the phones are reached by attribute. Changing the arity
    outright would have broken the two call sites silently in the
    worst possible place.
    """

    def __new__(cls, names, domains, phones=()):
        self = super().__new__(cls, (names, domains))
        self.names, self.domains = names, domains
        self.phones = set(phones or ())
        return self


def redact(text, names, domains, phones=()):
    """Take the client out of a string.

    `phones` defaults to empty so the signature stays compatible, but
    every caller inside this module passes them — a redaction that
    collected numbers and did not use them would be the same bug as
    collecting the name and not using it, which is how this module
    failed its first test.
    """
    if not text:
        return text
    out = redact_phones(str(text), phones)
    # Longest first, so "Tata Steel Limited" is replaced whole rather
    # than leaving "Steel Limited" behind after "Tata" went.
    for name in sorted(names, key=len, reverse=True):
        out = re.sub(r'\b' + re.escape(name) + r'\b', MASK, out,
                     flags=re.IGNORECASE)
    for domain in sorted(domains, key=len, reverse=True):
        out = re.sub(r'[\w.+-]+@' + re.escape(domain), MASK_DOMAIN,
                     out, flags=re.IGNORECASE)
        out = re.sub(re.escape(domain), MASK_DOMAIN, out,
                     flags=re.IGNORECASE)
    return out


def find_leaks(text, names, domains, phones=()):
    """Everything identifying still present. Empty list means clean."""
    found = list(find_phone_leaks(text, phones))
    probe = (text or '')
    for name in names:
        if re.search(r'\b' + re.escape(name) + r'\b', probe,
                     flags=re.IGNORECASE):
            found.append(name)
    for domain in domains:
        if re.search(re.escape(domain), probe, flags=re.IGNORECASE):
            found.append(domain)
    return found


def assert_clean(parts, names, domains, phones=(), *,
                 what='this message'):
    """Refuse to let identifying detail out.

    `parts` is everything a vendor would see — subject, body, and
    the names of any attachments. Raises rather than redacting at
    this stage: reaching here with a leak means the redaction missed
    something, and quietly patching it over would hide the gap
    instead of surfacing it.
    """
    leaks = []
    for part in parts:
        leaks.extend(find_leaks(part, names, domains, phones))
    if leaks:
        raise LeakError(
            f'{what} still names the client ('
            f'{", ".join(sorted(set(leaks))[:4])}'
            f'{"…" if len(set(leaks)) > 4 else ""}'
            f'). A vendor must not learn whose job this is — nothing '
            f'was sent.')
    return True


def vendor_view(rate_request, *, line, rfq, account=None, lead=None,
                consignee=None):
    """What a vendor is allowed to see of a rate request.

    Used for the outgoing mail and for the screen that shows what
    was sent. One function for both, because a screen that renders
    the unmasked version is a leak with extra steps — somebody
    screenshots it into a reply.
    """
    secrets = secrets_for(
        account=account, lead=lead, consignee=consignee,
        # The route is what the vendor is being asked to price.
        keep=[line.origin, line.destination,
              getattr(rfq, 'origin', None),
              getattr(rfq, 'destination', None)])

    def clean(v):
        return redact(v, secrets.names, secrets.domains, secrets.phones)

    return {
        # The T-series number only. The customer reference would let
        # two vendors line their quotes up against one job.
        'reference': (rfq.vendor_crn or '') if rfq else '',
        'service': clean(line.service),
        'scope': clean(line.scope),
        'origin': clean(line.origin),
        'destination': clean(line.destination),
        'cargo': clean(line.cargo),
        'required_by': (str(line.required_by)
                        if line.required_by else ''),
        'vendor_name': rate_request.vendor_name,
        'due_by': (str(rate_request.due_by)
                   if rate_request.due_by else ''),
        'note': clean(rate_request.request_note),
    }
