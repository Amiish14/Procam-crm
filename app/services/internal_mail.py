"""Our own mail, talking to us. Not an enquiry, and not a lead.

CRM-FINAL-13NOV item 10. The tender digest -- "Procam Logistics
owner: ... / Current bid status ... / Mr ... please create / confirm
the CRM lead ...", signed "Procam Tech team" -- became a lead called
"To review", carrying "Immediate sender is a Procam address; could not
identify external customer".

WHY THAT HAPPENED, AND WHY THE CODE WAS RIGHT UNTIL NOW

`mail_ingest.rescue_internal_forward` exists because a colleague
forwarding a customer's enquiry has every visible address on our own
domain, so the classifier calls it internal and nothing is created --
losing exactly the enquiries somebody cared enough to pass on. Its
docstring ends: "It never returns 'drop it'. That is the whole point."

That is correct for a forwarded enquiry and wrong for this. A digest
has no external customer because **there is no customer in it**: the
customer is on the tender lead that already exists, and the mail is a
notice about that lead. "Could not identify external customer" was a
true sentence about a mail that never had one.

THE THREE ANSWERS, IN ORDER

    attach   the notice names a tender, a customer or a subject we
             already hold -- it goes on that lead's trail, which is
             where somebody looking at the tender will find it.
    log      internal, names nothing we hold: Mail ingest, outcome
             `skipped`, reason "internal, not a lead". Visible,
             re-processable, and not on anybody's board.
    nothing  it is not internal, or it does not read as a notice.
             The rescue runs exactly as before.

NOT A KEYWORD LIST ON ITS OWN

Two things have to hold: the mail is from us (our domains, or an
address an administrator has named), **and** it reads as a notice
rather than as an enquiry. A colleague's covering note on a real
customer RFQ is from us too, and it must still rescue the enquiry
inside it -- so a mail carrying an external customer's address in the
quoted body is never treated as a notice here, whatever its wording.

NO ADDRESSES IN THIS FILE

The domains come from `mail_policy`. Any further sender -- a reporting
robot, a tech-team alias -- is an Admin setting read at the time, so
naming one is a screen and not a release.
"""
from __future__ import annotations

import re

#: The Admin setting naming senders whose mail is always a notice.
SENDER_SETTING = 'internal_notice_senders'

#: What our own notices say. Each is a phrase, not a word: "owner" and
#: "status" are in half the mail in the mailbox.
NOTICE_PATTERNS = (
    r'please\s+create\s*/?\s*(?:or\s+)?confirm\s+the\s+crm\s+lead',
    r'please\s+(?:create|confirm|update)\s+the\s+(?:crm\s+)?lead',
    r'current\s+bid\s+status',
    r'bid\s+status\s*[:\-]',
    r'procam\s+logistics\s+owner\s*[:\-]',
    r'tender\s+digest',
    r'daily\s+(?:tender|bid|status)\s+(?:digest|report|summary)',
    r'this\s+is\s+an\s+automated\s+(?:mail|message|digest)',
    r'procam\s+tech\s+team',
    r'do\s+not\s+reply\s+to\s+this\s+(?:mail|message)',
    r'crm\s+lead\s+(?:no|number|ref)\b',
)

_NOTICE = tuple(re.compile(p, re.I) for p in NOTICE_PATTERNS)

#: How much of the body to read. A notice says so at the top; the rest
#: is a table of tenders whose words would match anything.
_HEAD = 1500

ATTACH = 'attach'
LOG = 'log'
NOTHING = 'nothing'

#: The ingest-log reason, which the Mail ingest screen shows and the
#: archive script matches on.
REASON = 'internal, not a lead'


def _domains():
    try:
        from app.services import mail_policy
        return tuple(mail_policy.BUILTIN_DOMAINS)
    except Exception:                                     # noqa: BLE001
        return ()


def named_senders():
    """Addresses an administrator has declared to be notices."""
    try:
        from app.services import admin_settings
        raw = admin_settings.get(SENDER_SETTING)
    except Exception:                                     # noqa: BLE001
        return ()
    if isinstance(raw, str):
        raw = [part for part in re.split(r'[,;\s]+', raw) if part]
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(x).strip().lower() for x in raw if str(x).strip())


def from_us(address):
    """Is this address ours, or one an administrator has named?"""
    text = (address or '').strip().lower()
    if not text or '@' not in text:
        return False
    if text in named_senders():
        return True
    domain = text.rsplit('@', 1)[-1]
    return any(domain == ours or domain.endswith('.' + ours)
               for ours in _domains())


def reads_as_a_notice(subject='', body=''):
    """The phrase that makes this a notice, or ''."""
    text = f"{subject or ''}\n{(body or '')[:_HEAD]}"
    for pattern in _NOTICE:
        found = pattern.search(text)
        if found:
            return found.group(0).strip().lower()
    return ''


def carries_an_external_customer(body='', internal_domains=()):
    """Is there a customer's own ADDRESS inside this mail?

    Asked before anything is called a notice. A colleague's covering
    note on a real customer RFQ is from us and may well say "please
    create the lead" -- and the enquiry inside it must still be
    rescued. `mail_ingest.client_in_forward` is the same reader the
    rescue itself uses, so the two cannot disagree about whether
    there is a customer in there.

    An ADDRESS and not a name. A digest names customers -- that is
    its content, a table of tenders and whose they are -- so a company
    name in the body says nothing about whether this is an enquiry. A
    forwarded enquiry worth rescuing carries the customer's own
    address in its quoted From, which a digest never does.
    """
    try:
        from app.services import mail_ingest
        found = mail_ingest.client_in_forward(
            body or '', internal_domains=internal_domains or _domains())
    except Exception:                                     # noqa: BLE001
        return False
    return bool((found.get('email') or '').strip())


def decide(msg, body_text='', *, sender=None, internal_domains=()):
    """(what, lead_id, why) for one message.

    `what` is ATTACH, LOG or NOTHING. Never raises: an answer this
    module cannot give leaves the existing rescue to run, which is the
    behaviour that was there before it.
    """
    try:
        subject = (msg or {}).get('subject') or ''
    except Exception:                                     # noqa: BLE001
        subject = ''
    address = sender or ''
    if not address:
        try:
            from app.services import lead_intake as li
            address = li.sender(msg) or ''
        except Exception:                                 # noqa: BLE001
            address = ''

    if not from_us(address):
        return NOTHING, None, ''
    phrase = reads_as_a_notice(subject, body_text)
    if not phrase:
        return NOTHING, None, ''
    if carries_an_external_customer(body_text, internal_domains):
        # From us, worded like a notice, and there is a customer in
        # the body. The enquiry wins: that is the case the rescue was
        # written for.
        return NOTHING, None, ''

    lead_id, how = existing_lead_for(msg, body_text)
    if lead_id:
        return ATTACH, lead_id, f'{REASON}; it is about lead #{lead_id} ({how})'
    return LOG, None, f'{REASON} (it reads as a notice: "{phrase}")'


def existing_lead_for(msg, body_text=''):
    """(lead_id, how) for the lead a notice is about, or (None, '').

    Three keys, strongest first. A tender number is exact and appears
    in every mail of its set; a customer name is a name; a normalised
    subject is a guess and is windowed.
    """
    subject = (msg or {}).get('subject') or ''
    text = f'{subject}\n{body_text or ""}'

    found = _by_portal_key(subject, body_text)
    if found:
        return found, 'the same tender number'
    found = _by_customer(text)
    if found:
        return found, 'the customer named in it'
    found = _by_subject(subject)
    if found:
        return found, 'the same subject'
    return None, ''


def _open_leads():
    from app import Lead
    from app.services import sales_rules

    return (Lead.query
            .filter(Lead.is_archived.isnot(True),
                    Lead.junk_deleted.isnot(True),
                    Lead.merged_into_id.is_(None),
                    Lead.stage.in_(sales_rules.open_stages())))


def _by_portal_key(subject, body_text):
    """The tender or portal number, which every mail of a set carries."""
    try:
        from app.services import quote_due
        key = quote_due.portal_key(subject or '', body_text or '')
    except Exception:                                     # noqa: BLE001
        return None
    if not key:
        return None
    try:
        row = (_open_leads()
               .filter_by(portal_key=key)
               .order_by(_lead_id_asc()).first())
    except Exception:                                     # noqa: BLE001
        return None
    return getattr(row, 'id', None)


def _lead_id_asc():
    from app import Lead
    return Lead.id.asc()


def _by_customer(text):
    """A lead whose account or company name is named in the notice.

    Only names long enough to mean something: a three-letter company
    name appears inside other words, and a notice listing twenty
    tenders would match on the shortest of them.
    """
    from app.services import company_match

    rows = _open_leads().all()
    low = (text or '').lower()
    best = None
    for lead in rows:
        name = (lead.company or '').strip()
        if len(name) < 5 or company_match.norm(name) == '':
            continue
        if name.lower() in low:
            # The longest match wins: "BEML Limited" over "BEML".
            if best is None or len(name) > best[1]:
                best = (lead.id, len(name))
    return best[0] if best else None


def _by_subject(subject):
    from app.services import lead_thread

    text = (subject or '').strip()
    if len(text) < 10:
        return None
    try:
        from app.services import lead_intake as li
        from app.services import lead_merge as lm
        key = lm.thread_key(subject=li.match_subject(text), domain='')
    except Exception:                                     # noqa: BLE001
        return None
    if not key:
        # No domain, so `thread_key` gives nothing: fall back to the
        # stored key of a lead whose own subject normalises the same.
        return _by_normalised_subject(text)
    return lead_thread.find_by_key(key)


def _by_normalised_subject(subject):
    from app.services import lead_intake as li

    want = li.match_subject(subject).strip().lower()
    if len(want) < 10:
        return None
    for lead in _open_leads().all():
        theirs = li.match_subject(
            lead.original_email_subject or '').strip().lower()
        if theirs and theirs == want:
            return lead.id
    return None
