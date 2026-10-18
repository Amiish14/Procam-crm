"""
Which account a sender belongs to.

The measured rate on 30 days of real mail was 48 per cent: of 740
senders, 356 resolved. Two rules did all the work — the account's
email-domain list (294) and the contact table (62) — and three more
existed in the code and never fired once. Website-domain matching,
GSTIN matching and company-name matching contributed nothing,
because the first two rules settle it or nothing does.

Everything a mail cannot be matched to is a lead with no owner, or
an owner chosen by a fallback rather than by knowing the customer.
So the match rate is the number that matters: routing an unmatched
lead well is worth less than matching it in the first place.

Five changes:

    free mail       a domain rule must not fire on gmail.com. Two
                    customers with Gmail addresses are not the same
                    account, and `@gmail.com` on an account's domain
                    list would collect the entire internet. For those
                    senders only the exact contact address counts.

    website         `Company.website` holds a domain for most
                    accounts and was reached only after the domain
                    list missed — which it does for the 384 unmapped.

    GSTIN           an exact, government-issued identifier sitting in
                    the signature of a great many Indian business
                    mails.

    company name    normalised, and only when exactly one account
                    matches. Two candidates means a person decides.

    learned links   what a correction taught us. When somebody links
                    a lead to an account or reassigns it, the sender
                    is remembered, so the next mail from that person
                    matches with nobody doing anything.

Order matters and is deliberate: what a person told us, then what we
know exactly, then what we infer. See `resolve`.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

log = logging.getLogger(__name__)

#: A domain that belongs to everybody belongs to no account. Matching
#: on one would put every Gmail customer on whichever account listed
#: it first.
FREE_MAIL = {
    'gmail.com', 'googlemail.com', 'yahoo.com', 'yahoo.co.in',
    'yahoo.co.uk', 'ymail.com', 'rocketmail.com',
    'outlook.com', 'hotmail.com', 'hotmail.co.uk', 'live.com',
    'msn.com', 'aol.com', 'icloud.com', 'me.com', 'mac.com',
    'protonmail.com', 'proton.me', 'zoho.com', 'gmx.com', 'mail.com',
    'rediffmail.com', 'rediff.com', 'sify.com', 'indiatimes.com',
    'vsnl.net', 'vsnl.com', 'bsnl.in', 'airtelmail.in',
    'qq.com', '163.com', '126.com', 'yandex.com', 'yandex.ru',
}

#: Rules, in the order `resolve` tries them. Named so the survey and
#: the audit row can say which one fired.
RULES = ('learned email', 'contact email', 'learned domain',
         'account email domain', 'account website domain', 'GSTIN',
         'company name')

UNMAPPED = 'unmapped'


def domain_of(address):
    addr = (address or '').strip().lower()
    if '<' in addr and '>' in addr:
        addr = addr[addr.rfind('<') + 1:addr.rfind('>')].strip()
    if addr.count('@') != 1:
        return ''
    return addr.rsplit('@', 1)[1].strip().strip('.')


def is_free_mail(domain):
    d = (domain or '').strip().lower()
    return bool(d) and (d in FREE_MAIL
                        or any(d.endswith('.' + f) for f in FREE_MAIL))


def normalise_address(address):
    addr = (address or '').strip().lower()
    if '<' in addr and '>' in addr:
        addr = addr[addr.rfind('<') + 1:addr.rfind('>')].strip()
    return addr


# ── the learned links ────────────────────────────────────────────────
def _learned(kind, value):
    from app.models.assignment import SenderAccountLink
    try:
        return (SenderAccountLink.query
                .filter_by(kind=kind, value=(value or '').lower()).first())
    except Exception:
        from app import db
        db.session.rollback()
        return None


def _hit(link):
    """Count a use, so a link nobody ever matches can be found."""
    try:
        from app import db
        link.hits = (link.hits or 0) + 1
        link.last_hit_at = datetime.utcnow()
        db.session.flush()
    except Exception:
        pass


def learn(sender_email, account_id, *, lead_id=None, actor=None,
          reason='', commit=True):
    """Remember that this sender belongs to this account.

    Called when a person corrects the CRM, never when the CRM
    guesses: a guess that teaches itself compounds. The domain is
    learnt too, unless it is free mail, where one address says
    nothing about the next.

    Returns the rows written.
    """
    from app import db
    from app.models.assignment import BY_DOMAIN, BY_EMAIL, SenderAccountLink

    addr = normalise_address(sender_email)
    if not addr or '@' not in addr or not account_id:
        return []

    pairs = [(BY_EMAIL, addr)]
    domain = domain_of(addr)
    if domain and not is_free_mail(domain):
        pairs.append((BY_DOMAIN, domain))

    written = []
    for kind, value in pairs:
        try:
            row = SenderAccountLink.query.filter_by(kind=kind,
                                                    value=value).first()
            if row is not None:
                if row.account_id == account_id:
                    continue
                # A correction that contradicts an earlier one is the
                # later person's call; the previous value goes in the
                # audit trail rather than being lost.
                was = row.account_id
                row.account_id = account_id
                row.learned_by = actor
                row.reason = (reason or '')[:200]
                _audit('assignment.link_repointed', value, was, account_id,
                       actor, reason)
            else:
                row = SenderAccountLink(
                    kind=kind, value=value, account_id=account_id,
                    learned_from_lead=lead_id, learned_by=actor,
                    reason=(reason or '')[:200])
                db.session.add(row)
                _audit('assignment.link_learned', value, None, account_id,
                       actor, reason)
            written.append(row)
        except Exception:
            db.session.rollback()
            log.exception('could not learn %s=%s', kind, value)
    if commit and written:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            return []
    return written


def _audit(action, value, was, now, actor, reason):
    try:
        from app.services import audit
        audit.record(action, 'SenderAccountLink', value,
                     old=({'account_id': was} if was else None),
                     new={'account_id': now}, actor=actor,
                     reason=(reason or 'learnt from a correction')[:200],
                     commit=False)
    except Exception:
        pass


# ── the resolver ─────────────────────────────────────────────────────
def resolve(from_addr, *, company_name=None, text=None, count_hit=True):
    """(account, rule) for a sender. (None, 'unmapped') when nothing
    matches, which is an answer and not a failure.

    What a person told us first, then what we know exactly, then what
    we infer.
    """
    from app import Company, Contact, db
    from app.models.assignment import BY_DOMAIN, BY_EMAIL

    addr = normalise_address(from_addr)
    domain = domain_of(addr)
    free = is_free_mail(domain)

    def account(account_id):
        return db.session.get(Company, account_id) if account_id else None

    # 1. a correction somebody made, for this exact person
    if addr:
        link = _learned(BY_EMAIL, addr)
        if link is not None:
            found = account(link.account_id)
            if found is not None:
                if count_hit:
                    _hit(link)
                return found, 'learned email'

    # 2. the contact table — the one rule that works for free mail
    if addr:
        try:
            contact = Contact.query.filter(Contact.email.ilike(addr)).first()
            if contact is not None:
                found = account(getattr(contact, 'account_id', None)
                                or getattr(contact, 'company_id', None))
                if found is not None:
                    return found, 'contact email'
        except Exception:
            db.session.rollback()

    # Everything below is a domain or a name. None of it may fire on
    # free mail: two customers on gmail.com are two customers.
    if domain and not free:
        link = _learned(BY_DOMAIN, domain)
        if link is not None:
            found = account(link.account_id)
            if found is not None:
                if count_hit:
                    _hit(link)
                return found, 'learned domain'

        try:
            for company in Company.query.filter(
                    Company.email_domains.isnot(None)).limit(5000).all():
                listed = [str(d).lower().lstrip('@').strip()
                          for d in (company.email_domains or [])]
                if domain in listed:
                    return company, 'account email domain'
        except Exception:
            db.session.rollback()

        try:
            hit = _by_website(domain)
            if hit is not None:
                return hit, 'account website domain'
        except Exception:
            db.session.rollback()

    # 3. a GSTIN in the body or the signature — exact, and issued by
    #    somebody other than us
    if text:
        try:
            from app.services import lead_intake as li
            for number in li.gstins(text):
                hit = Company.query.filter(Company.gstin == number).first()
                if hit is not None:
                    return hit, 'GSTIN'
        except Exception:
            db.session.rollback()

    # 4. the name, normalised, and only when it is unambiguous
    if company_name:
        hit = _by_name(company_name)
        if hit is not None:
            return hit, 'company name'

    return None, UNMAPPED


def _website_domain(value):
    """The bare domain inside whatever somebody typed in `website`."""
    raw = (value or '').strip().lower()
    if not raw:
        return ''
    raw = re.sub(r'^\w+://', '', raw)
    raw = raw.split('/')[0].split('?')[0].strip()
    if raw.startswith('www.'):
        raw = raw[4:]
    return raw.strip('.')


def _by_website(domain):
    """An account whose website is this domain.

    Compared as domains, not with LIKE. `%adani%` matched
    `adani.com`, `adaniports.com` and `notadani.com` alike, which is
    why this rule could never be trusted and so was never reached.
    """
    from app import Company

    want = (domain or '').lower().lstrip('.')
    if not want or is_free_mail(want):
        return None
    for company in Company.query.filter(
            Company.website.isnot(None)).limit(5000).all():
        theirs = _website_domain(company.website)
        if not theirs or is_free_mail(theirs):
            continue
        if theirs == want or want.endswith('.' + theirs) \
                or theirs.endswith('.' + want):
            return company
    return None


#: Geography that is part of a legal name but not of an identity.
#: "Siemens India" and "Siemens" are the same customer to a freight
#: forwarder. Stripped only here: `company_match.norm` is shared with
#: account de-duplication and with the TMS integration's find-or-
#: create, and widening it there would change which accounts those
#: merge — a much larger decision than improving one match rule.
_GEO_SUFFIXES = (' india', ' asia', ' apac', ' middle east', ' me',
                 ' south asia', ' bharat')


def strict_norm(name):
    """`company_match.norm`, and the geography, until neither bites.

    They have to alternate. "Tata Steel Limited, India" normalises to
    "tata steel limited india" — the legal suffix is not at the end,
    so norm leaves it — and stripping the geography once exposes a
    "limited" that nothing then removes.
    """
    from app.services import company_match

    s = company_match.norm(name)
    for _pass in range(6):
        before = s
        for suf in _GEO_SUFFIXES:
            if s.endswith(suf) and len(s) > len(suf) + 2:
                s = s[: -len(suf)].strip()
        s = company_match.norm(s)
        if s == before:
            break
    return s


def _by_name(company_name):
    """An account whose normalised name is exactly this one.

    One candidate or nothing. Two accounts that normalise alike is a
    merge somebody should do deliberately, not a match to guess at —
    and that is doubly true of the looser pass, which is tried only
    when the strict one finds nothing.
    """
    from app import Company
    from app.services import company_match

    raw = (company_name or '').strip()
    if len(company_match.norm(raw)) < 3:
        return None
    try:
        accounts = Company.query.filter(Company.is_active.is_(True)).all()
    except Exception:
        from app import db
        db.session.rollback()
        return None

    for normalise in (company_match.norm, strict_norm):
        want = normalise(raw)
        if not want or len(want) < 3:
            continue
        found = [c for c in accounts if normalise(c.name) == want]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            log.info('company name %r matches %d accounts — leaving it '
                     'to a person', company_name, len(found))
            return None
    return None
