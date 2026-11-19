"""Us. The addresses and the names that are Procam, not a customer.

UAT 19 Nov item 1. Lead #10029 showed up in Find a person as a person
called "Procam Forward", company "Unknown", reachable on
`procamadmin@procamgroup.in`. It is our own admin address on our own
lead, and a search that offers it as somebody to ring is offering a
colleague's mailbox as a sales contact.

WHY THIS FILE EXISTS RATHER THAN A FIFTH LIST

Four places already knew some of this and none knew all of it:

    app/services/mail_signals.py          OUR_DOMAINS, two of them
    app/services/mail_policy.py           BUILTIN_DOMAINS, the same two
    app/data_quality/definitions.py       INTERNAL_EMAIL_DOMAINS, ditto
    app/services/lead_intake_db.py        procamgroup.in alone

`procamlogitech.com` -- the host the CRM is actually served from -- and
`orsayinternational.com` were in none of them. So this module is the
union of what those lists hold and what they were missing, and it reads
them rather than copying them: a domain added to any of them is ours
here too, without a second edit. That is the same reason
`people_search.is_system_address` reads `portal_rfq`'s list instead of
keeping its own.

OWN IS NOT THE SAME AS SYSTEM

`is_system_address` answers "is this a robot" -- a portal's delivery
address, a noreply. This answers "is this us". Both mean "not a customer
person", and they are asked separately because they are fixed
differently: a portal address is a portal's, and ours is a lead that
somebody filed with our own mailbox in the PIC box.
"""

#: The ones named in the UAT, which is the authority for this list.
#: `procamlogitech.com` is where the CRM is served; `orsayinternational`
#: is the group's other trading name.
NAMED = (
    'procamgroup.in',
    'procamlogistics.com',
    'procamlogitech.com',
    'orsayinternational.com',
)

#: A PIC whose NAME starts with one of these is us however it is
#: spelled and whatever address it carries -- "Procam Forward",
#: "Procam Admin", "Procam Logistics Pvt Ltd". Checked as the first
#: word, so a customer called "Procamol" is not caught.
OWN_NAME_WORDS = ('procam', 'orsay')


def own_domains():
    """Every domain that is ours, from here and from the older lists.

    Read at call time, not import time: the older lists live in modules
    that import the app, and a cycle at import is how a rights check or
    a search ends up answering "no" because its own module failed to
    load.
    """
    out = set(NAMED)
    for module, attr in (
            ('app.services.mail_signals', 'OUR_DOMAINS'),
            ('app.services.mail_policy', 'BUILTIN_DOMAINS'),
            ('app.data_quality.definitions', 'INTERNAL_EMAIL_DOMAINS')):
        try:
            mod = __import__(module, fromlist=[attr])
            for said in getattr(mod, attr, ()) or ():
                text = str(said or '').strip().lower().lstrip('@')
                if text:
                    out.add(text)
        except Exception:                                  # noqa: BLE001
            continue
    return frozenset(out)


def domain_of(address):
    said = str(address or '').strip().lower().strip('<>,;')
    if '@' not in said:
        return ''
    return said.rsplit('@', 1)[1].strip().strip('.')


def is_own_address(address):
    """Is this address on one of our own domains?

    Subdomains count: `mail.procamgroup.in` is ours. Matched on the
    domain's tail with a dot in front, so `notprocamgroup.in` is not.
    """
    domain = domain_of(address)
    if not domain:
        return False
    for ours in own_domains():
        if domain == ours or domain.endswith('.' + ours):
            return True
    return False


def is_own_name(name):
    """Is this PIC OUR name rather than a person's?

    The first word only. "Procam Forward" and "Procam Admin" are ours;
    somebody genuinely surnamed Procamo is not, because the comparison
    is on whole words.
    """
    words = [w.strip('(),;:.').lower()
             for w in str(name or '').replace('-', ' ').split()]
    return bool(words) and words[0] in OWN_NAME_WORDS


def is_us(name=None, *addresses):
    """(True, why) when this person is Procam rather than a customer.

    Returns the reason as well as the verdict, so a screen or a script
    can say WHICH test caught it -- "our own domain" and "named Procam"
    are fixed differently on the lead.
    """
    for address in addresses:
        for one in str(address or '').replace(';', ',').split(','):
            if is_own_address(one):
                return True, f'{domain_of(one)} is our own domain'
    if is_own_name(name):
        first = str(name).split()[0]
        return True, f'the PIC is named "{first} …", which is us'
    return False, ''
