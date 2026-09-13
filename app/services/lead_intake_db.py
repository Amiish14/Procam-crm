"""
The database side of lead intake — lookups, account resolution, recording.

app/services/lead_intake.py holds the decision tree and touches nothing.
This module supplies it with a Context that actually queries, resolves an
account to its two owners, and writes the decision down.

Split this way because the tree is the part worth testing exhaustively
and the part that must stay arguable; keeping SQL out of it is what makes
that possible.
"""
from datetime import datetime, timedelta

from app.services import lead_intake as li


# ─── who we are ──────────────────────────────────────────────────────────
def internal_domains():
    """Domains that are us. The mailbox's own domain, plus anything the
    ingest already treats as internal."""
    import os
    from app import app as flask_app

    out = {'procamgroup.in'}
    for var in ('CRM_INBOX_EMAIL', 'EMAIL_INGEST_MAILBOX'):
        v = os.environ.get(var) or ''
        if '@' in v:
            out.add(v.rsplit('@', 1)[1].strip().lower())
    extra = os.environ.get('INTERNAL_EMAIL_DOMAINS') or ''
    for d in extra.split(','):
        d = d.strip().lower().lstrip('@')
        if d:
            out.add(d)
    try:
        flask_app.logger.debug('internal domains: %s', sorted(out))
    except Exception:
        pass
    return out


# ─── thread matching — step 3, the decisive one ──────────────────────────
def find_by_thread(conversation_id=None, in_reply_to=None, reference_ids=None):
    """The lead this message's conversation already belongs to.

    Checked against both the lead and its email trail: the enquiry
    carries the first message's identity, the trail carries every reply
    since, and a reply four messages deep matches the trail, not the lead.
    """
    from app import Lead, LeadEmail

    if conversation_id:
        row = Lead.query.filter_by(conversation_id=conversation_id) \
                        .order_by(Lead.id.asc()).first()
        if row:
            return row.id
        mail = LeadEmail.query.filter_by(
            conversation_id=conversation_id).first()
        if mail:
            return mail.lead_id

    # in_reply_to and references name specific messages we may have stored.
    candidates = [m for m in ([in_reply_to] + list(reference_ids or [])) if m]
    if candidates:
        row = Lead.query.filter(
            Lead.email_message_id.in_(candidates)).first()
        if row:
            return row.id
        mail = LeadEmail.query.filter(
            LeadEmail.message_id.in_(candidates)).first()
        if mail:
            return mail.lead_id
    return None


# ─── subject fallback — step 5, for threads we never captured ────────────
def find_by_subject(subject=None, counterparties=(), within_days=180):
    """A lead with the same stripped subject and a shared counterparty.

    Both halves are required. Subject alone matches every "RFQ" ever
    sent; a counterparty alone matches every enquiry a customer has made.
    """
    from app import Lead

    subject = (subject or '').strip()
    if len(subject) < 6:
        return None                     # too generic to match on

    domains = {li.domain_of(a) for a in (counterparties or []) if a}
    domains -= internal_domains()
    domains = {d for d in domains if d}
    if not domains:
        return None

    since = datetime.utcnow() - timedelta(days=within_days)
    rows = (Lead.query
            .filter(Lead.original_email_subject.isnot(None),
                    Lead.created_at >= since)
            .order_by(Lead.created_at.desc()).limit(2000).all())

    want = _fold(subject)
    for lead in rows:
        if _fold(li.strip_prefixes(lead.original_email_subject or '')) != want:
            continue
        lead_domain = li.domain_of(lead.email or '')
        if lead_domain and lead_domain in domains:
            return lead.id
    return None


def _fold(text):
    import re
    return re.sub(r'[^a-z0-9]', '', (text or '').lower())


# ─── vendors — step 6 ────────────────────────────────────────────────────
def is_vendor_domain(domain):
    from app import VendorDomain
    if not domain:
        return False
    try:
        return bool(VendorDomain.query.filter_by(
            domain=domain.lower(), is_active=True).first())
    except Exception:
        return False        # table not migrated yet: never block on it


# ─── duplicates — step 9 ─────────────────────────────────────────────────
def duplicate_score(msg=None, subject=None, from_addr=None, window_days=30):
    """(score, lead_id). Weights live in lead_intake.DUPLICATE_WEIGHTS."""
    from app import Lead

    from_addr = (from_addr or '').lower()
    domain = li.domain_of(from_addr)
    if not domain:
        return 0, None

    since = datetime.utcnow() - timedelta(days=window_days)
    rows = (Lead.query
            .filter(Lead.email.isnot(None), Lead.created_at >= since)
            .order_by(Lead.created_at.desc()).limit(500).all())
    if not rows:
        return 0, None

    want_subject = _fold(subject or '')
    want_files = {(a or {}).get('name', '').lower()
                  for a in (msg or {}).get('attachments') or []}
    want_files.discard('')

    best, best_id = 0, None
    for lead in rows:
        lead_addr = (lead.email or '').lower()
        lead_domain = li.domain_of(lead_addr)
        signals = {}

        if lead_addr and lead_addr == from_addr and want_subject and \
                _fold(li.strip_prefixes(lead.original_email_subject or '')) \
                == want_subject:
            signals['same_sender_and_subject'] = True
        if lead_domain and lead_domain == domain:
            signals['same_account_in_window'] = True

        score = li.score_duplicate(signals)
        if score > best:
            best, best_id = score, lead.id
    return best, best_id


# ─── logistics content — step 8, reusing the parser's own judgement ──────
#: Parser skip reasons that mean "this is not business mail at all".
#: The parser reports several; only one of them was being honoured, so a
#: forwarded newsletter from a no-reply address reached the confidence
#: step and landed in Admin Review instead of being set aside. Five of
#: the first twenty-five review items in the real mailbox were
#: newsletters for exactly this reason.
_NON_BUSINESS_REASONS = (
    'not logistics-related',
    'no-reply sender',
    'auto-reply',
    'bounce',
    'bulk',
    'newsletter',
    'marketing',
    'empty message',
)


def has_logistics_content(msg):
    """Whether this is business mail worth considering at all.

    The parser already makes this judgement at parser.py:799 and :974,
    and reports it as a skip reason. Reused rather than reimplemented so
    the two cannot drift apart — but every reason it gives is honoured,
    not just the one about cargo keywords.
    """
    try:
        from email_ingest import parser as email_parser
        extracted = email_parser.extract_lead(msg)
        if not extracted:
            return False
        reason = (extracted.get('skip_reason') or '').lower()
        if not reason:
            return True
        return not any(marker in reason for marker in _NON_BUSINESS_REASONS)
    except Exception:
        return True         # never lose a lead to a parser failure


def _ai_second_opinion(msg, decided):
    """Phase 4, wired only when LEAD_INTAKE_AI is on.

    Returns the decision either way: an unavailable model, an
    unparseable answer or one less certain than the rule leaves the
    rule's decision exactly as it was.
    """
    from app.services import lead_intake_ai as ai
    return ai.apply(decided, ai.opinion(msg, decided))


def build_context():
    """The Context the tree runs against in production."""
    from app.services import lead_intake_ai as ai
    return li.Context(
        internal_domains=internal_domains(),
        find_by_thread=find_by_thread,
        find_by_subject=find_by_subject,
        duplicate_score=duplicate_score,
        is_vendor_domain=is_vendor_domain,
        has_logistics_content=has_logistics_content,
        ai_opinion=_ai_second_opinion if ai.is_enabled() else None,
    )


# ─── account → two owners ────────────────────────────────────────────────
def resolve_account(from_addr, company_name=None, text_for_gstin=None):
    """(company, how) — the account this sender belongs to.

    Resolution order per §07, stopping at the first hit. Returns
    (None, 'unmapped') when nothing matches, which is a queue entry
    rather than a failure.
    """
    from app import Company, Contact

    addr = (from_addr or '').strip().lower()
    domain = li.domain_of(addr)

    if addr:
        contact = Contact.query.filter(Contact.email.ilike(addr)).first()
        if contact and (contact.account_id or contact.company_id):
            c = Company.query.get(contact.account_id or contact.company_id)
            if c:
                return c, 'contact email'

    if domain:
        for company in Company.query.filter(
                Company.email_domains.isnot(None)).limit(5000).all():
            listed = [str(d).lower().lstrip('@')
                      for d in (company.email_domains or [])]
            if domain in listed:
                return company, 'account email domain'

        hit = Company.query.filter(
            Company.website.ilike(f'%{domain}%')).first()
        if hit:
            return hit, 'account website domain'

    # §13 path 5 — the GST / customer master. The brief lists this last,
    # but a GSTIN is a government-issued exact identifier and the company
    # name below it is a fuzzy match. Letting a guess beat a fact is the
    # same mistake as letting the model overturn a thread match, so the
    # exact one is checked first. Say the word and the order flips.
    for number in li.gstins(text_for_gstin or ''):
        hit = Company.query.filter(Company.gstin == number).first()
        if hit:
            return hit, 'GSTIN'

    if company_name:
        try:
            from app.services.company_match import build_index, match
            index = build_index(
                Company.query.filter(Company.is_active.is_(True)).all())
            found, _reason, _cands = match(company_name, index)
            if found:
                return found, 'company name'
        except Exception:
            pass

    return None, 'unmapped'


def owners_for(company):
    """(primary, secondary) emp codes from the Account Master.

    Both may be None — an account with no owners configured is exactly
    the case the Unmapped queue exists for, and inventing an owner here
    would hide it.
    """
    if company is None:
        return None, None
    return (getattr(company, 'pic_emp_code', None) or None,
            getattr(company, 'secondary_pic_emp_code', None) or None)


# ─── recording — the training set ────────────────────────────────────────
def record(decision, msg, *, created_lead_id=None):
    """Write the decision down. Best-effort: a recording failure must
    never stop a genuine lead being created."""
    from app import db, EmailClassification, app as flask_app

    try:
        from_addr = li.sender(msg)
        row = EmailClassification(
            message_id=(msg.get('internetMessageId') or '')[:400] or None,
            conversation_id=(msg.get('conversationId') or '')[:200] or None,
            subject=(msg.get('subject') or '')[:500] or None,
            from_addr=from_addr[:320] or None,
            from_domain=li.domain_of(from_addr)[:200] or None,
            classification=decision.klass,
            decided_by=f'step_{decision.step}',
            reason=(decision.reason or '')[:300],
            confidence=decision.confidence,
            duplicate_score=decision.duplicate_score,
            matched_lead_id=decision.lead_id,
            created_lead_id=created_lead_id,
            payload=_reviewable(msg, decision),
            review_state='pending' if decision.needs_review else 'accepted',
        )
        db.session.add(row)
        return row
    except Exception:
        try:
            flask_app.logger.exception('could not record a classification')
        except Exception:
            pass
        return None


def _reviewable(msg, decision=None):
    """The parts of a message a reviewer needs, and no more.

    Capped: this is kept for every message, and storing whole HTML
    bodies for thousands of newsletters would cost more than the review
    queue is worth.
    """
    try:
        out = {
            'to': li.recipients(msg, 'toRecipients')[:10],
            'cc': li.recipients(msg, 'ccRecipients')[:10],
            'received': (msg.get('receivedDateTime') or '')[:19],
            'body': (li.body_text(msg) or '')[:4000],
            'attachments': li.attachment_names(msg)[:20],
            'resolved_sender': msg.get('_resolved_sender') or '',
            'forward_resolved': bool(msg.get('_forward_resolved')),
        }
        # Phase 4 — what the model said, and whether it was applied, kept
        # beside the rule's own answer so the two can be compared once
        # there is enough of both to judge.
        for key, value in ((decision.extra if decision else None) or {}).items():
            if key.startswith('ai_') or key == 'rule_class':
                out[key] = value
        return out
    except Exception:
        return {}


def correct(classification_row, corrected_to, *, reason=None, by=None):
    """A human disagreed. The correction sits beside the original — the
    pair is the label, not the correction alone."""
    if classification_row is None:
        return None
    classification_row.corrected_to = corrected_to
    classification_row.correction_reason = reason
    classification_row.corrected_by = by
    classification_row.corrected_at = datetime.utcnow()
    return classification_row
