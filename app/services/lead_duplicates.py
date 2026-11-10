"""Is this enquiry already on the board? Four signals, four strengths.

CRM-WA-01 §2: "Show a 'possible duplicate' warning at lead creation
(same company name normalised, same email domain, same phone, same
GSTIN)."

WHY THE SIGNALS ARE NOT EQUAL, AND WHY THAT IS THE WHOLE DESIGN

A warning that cries wolf is a warning people click through, and the
four signals are nowhere near equally strong:

    GSTIN         one legal entity, exactly. Two leads sharing a
                  GSTIN are the same customer, full stop.
    phone         a person, nearly always. Shared switchboards exist
                  but a direct line is one human being.
    company name  normalised through `company_match.norm`, which
                  strips the legal forms. Strong, and the one that
                  produces the false positives everybody remembers:
                  RENEW POWER and RENEW PHOTOVOLTAICS normalise
                  apart, but plenty of real pairs do not.
    email domain  the weakest by a distance. `renew.com` carries two
                  legal entities; `gmail.com` carries the country. On
                  its own it means almost nothing, which is why it is
                  scored lowest and never shown alone as a "duplicate".

So each match carries its own strength and its own sentence, and the
screen shows what matched rather than a number. "Same GSTIN as #10291"
is actionable. "87% similar" is not.

FREE MAIL IS NOT A DOMAIN MATCH

`account_match.is_free_mail` is asked before the domain signal
counts. Two leads from two different customers who both used Gmail
are not related, and treating them as related would make the warning
fire on half the board.

THIS ONLY EVER WARNS

Nothing here blocks a save, and nothing here merges. §2 asks for a
warning at creation and an admin merge, and they are deliberately
separate: the person keying a lead in is not always the person who
can tell whether two enquiries are one job.
"""
from __future__ import annotations

import re

#: What each signal is worth, and what it means. Ordered strongest
#: first, which is also the order the screen shows them in.
GSTIN = 'gstin'
PHONE = 'phone'
NAME = 'company_name'
DOMAIN = 'email_domain'

SIGNALS = (
    (GSTIN, 95, 'the same GSTIN — one legal entity, exactly'),
    (PHONE, 80, 'the same phone number'),
    (NAME, 70, 'the same company name, once the legal form is stripped'),
    (DOMAIN, 35, 'the same email domain, which can be two entities'),
)

STRENGTH = {key: score for key, score, _what in SIGNALS}
MEANING = {key: what for key, _score, what in SIGNALS}

#: Below this, a match is not worth interrupting somebody for. One
#: domain match alone sits under it on purpose.
WORTH_SHOWING = 50

#: How many to show. A warning listing twenty leads is a warning
#: nobody reads.
LIMIT = 5


def norm_name(value):
    """A company name with the legal form stripped.

    `company_match.norm` rather than a second normaliser here: the
    account matcher, the duplicate report and this warning must agree
    about what "the same name" means, and three implementations would
    not.
    """
    try:
        from app.services import company_match
        return company_match.norm(value or '')
    except Exception:                                     # noqa: BLE001
        return (value or '').strip().lower()


def norm_phone(value):
    """The last ten digits, or ''.

    Ten because that is an Indian subscriber number: +91 98••• •••••,
    098••• ••••• and 98••• ••••• are one phone, and comparing the
    typed strings makes them three.
    """
    digits = re.sub(r'\D+', '', str(value or ''))
    if len(digits) < 10:
        return ''
    return digits[-10:]


def norm_gstin(value):
    """A GSTIN, upper-cased, or '' if it is not one.

    Validated through `lead_intake.valid_gstin`, so a half-typed
    number does not match another half-typed number.
    """
    text = re.sub(r'\s+', '', str(value or '')).upper()
    if not text:
        return ''
    try:
        from app.services import lead_intake
        return text if lead_intake.valid_gstin(text) else ''
    except Exception:                                     # noqa: BLE001
        return text if len(text) == 15 else ''


def domain_of(address):
    """The domain of an address, unless it is free mail."""
    try:
        from app.services import account_match
        domain = account_match.domain_of(
            account_match.normalise_address(address or ''))
        if not domain or account_match.is_free_mail(domain):
            return ''
        return domain
    except Exception:                                     # noqa: BLE001
        return ''


def _gstin_of(lead):
    """A lead's GSTIN, which lives on its account.

    `leads` has no GSTIN column — the GST master is on `companies` —
    so a lead's GSTIN is its account's. A lead with no account has
    none, and that is an absence rather than a mismatch.
    """
    account_id = getattr(lead, 'company_id', None)
    if not account_id:
        return ''
    try:
        from app import Company
        account = Company.query.get(account_id)
    except Exception:                                     # noqa: BLE001
        return ''
    return norm_gstin(getattr(account, 'gstin', '') if account else '')


def fingerprint(*, company=None, email=None, phone=None, gstin=None,
                lead=None):
    """The four comparable values, from a lead or from loose fields.

    One function for both so the warning shown while typing and the
    report run over the whole book cannot disagree about what they
    are comparing.
    """
    if lead is not None:
        company = company if company is not None else getattr(
            lead, 'company', '')
        email = email if email is not None else (
            getattr(lead, 'email', '')
            or getattr(lead, 'original_email_from', ''))
        phone = phone if phone is not None else (
            getattr(lead, 'phone', '') or getattr(lead, 'phone2', ''))
        gstin = gstin if gstin is not None else _gstin_of(lead)
    return {
        NAME: norm_name(company),
        DOMAIN: domain_of(email),
        PHONE: norm_phone(phone),
        GSTIN: norm_gstin(gstin),
    }


def compare(mine, theirs):
    """Which signals these two fingerprints share."""
    out = []
    for key, _score, _what in SIGNALS:
        value = mine.get(key)
        if value and value == theirs.get(key):
            out.append(key)
    return out


def score(matched):
    """One number for a set of matched signals.

    The strongest signal, plus a little for each one that agrees with
    it. Not a sum: two weak signals should not add up to a certainty,
    and a shared domain plus a shared name is still two readings of
    the same fact — they are usually the same company *or* usually
    not, together.
    """
    if not matched:
        return 0
    best = max(STRENGTH[key] for key in matched)
    return min(99, best + 5 * (len(matched) - 1))


def _live_leads(exclude_id=None, limit_days=None):
    from datetime import datetime, timedelta

    from app import Lead

    q = Lead.query.filter(Lead.is_archived.isnot(True),
                          Lead.junk_deleted.isnot(True),
                          Lead.merged_into_id.is_(None))
    if exclude_id:
        q = q.filter(Lead.id != exclude_id)
    if limit_days:
        q = q.filter(Lead.created_at
                     >= datetime.utcnow() - timedelta(days=limit_days))
    return q.all()


def check(*, company=None, email=None, phone=None, gstin=None,
          exclude_id=None, within_days=None, limit=LIMIT):
    """Leads that look like this one, strongest first.

    Returns a list of {lead_id, company, stage, owner, created,
    matched, why, score}. Empty when nothing is worth interrupting
    somebody for.
    """
    mine = fingerprint(company=company, email=email, phone=phone,
                       gstin=gstin)
    if not any(mine.values()):
        return []

    found = []
    for other in _live_leads(exclude_id=exclude_id,
                             limit_days=within_days):
        matched = compare(mine, fingerprint(lead=other))
        if not matched:
            continue
        points = score(matched)
        if points < WORTH_SHOWING:
            continue
        found.append({
            'lead_id': other.id,
            'company': (other.company or '')[:60],
            'stage': other.stage or '',
            'owner': other.assigned_to or '',
            'created': str(other.created_at or '')[:10],
            'matched': matched,
            'why': '; '.join(MEANING[key] for key in matched),
            'score': points,
        })
    found.sort(key=lambda row: (-row['score'], -row['lead_id']))
    return found[:limit]


def for_lead(lead, *, within_days=None, limit=LIMIT):
    """The same question asked about a lead already on file."""
    if lead is None:
        return []
    mine = fingerprint(lead=lead)
    return check(company=lead.company,
                 email=(lead.email or lead.original_email_from),
                 phone=(lead.phone or lead.phone2),
                 gstin=mine[GSTIN],
                 exclude_id=lead.id, within_days=within_days,
                 limit=limit)
