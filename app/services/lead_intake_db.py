"""
The database side of lead intake — lookups, account resolution, recording.

app/services/lead_intake.py holds the decision tree and touches nothing.
This module supplies it with a Context that actually queries, resolves an
account to its two owners, and writes the decision down.

Split this way because the tree is the part worth testing exhaustively
and the part that must stay arguable; keeping SQL out of it is what makes
that possible.
"""
import re
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
    """Whether this sender's domain, or a domain it sits under, is an
    active entry in the Vendor Master.

    A shipping line writes from mail.maersk.com as often as maersk.com,
    so the registered domain covers its subdomains — by whole labels, so
    notmaersk.com is never caught by maersk.com. A deactivated row never
    matches: deactivating is how an admin undoes a wrong entry.
    """
    from app import VendorDomain, db
    candidates = li.domain_and_parents(domain)
    if not candidates:
        return False
    try:
        # Only the domain column is selected, so matching keeps working
        # on a database that has not yet gained the Vendor Master's
        # newer columns — the classifier must not go quiet because a
        # screen's migration is pending.
        return bool(db.session.query(VendorDomain.domain)
                    .filter(VendorDomain.domain.in_(candidates),
                            VendorDomain.is_active.is_(True))
                    .first())
    except Exception:
        return False        # table not migrated yet: never block on it


# ─── duplicates — step 9 ─────────────────────────────────────────────────
#: How many recent leads a message is compared with. Duplicates are
#: recent by nature, and the scoring reads each candidate's text, so the
#: set stays bounded however large the lead table grows.
DUPLICATE_CANDIDATES = 500


def duplicate_score(msg=None, subject=None, from_addr=None, window_days=30):
    """(score, lead_id). Weights live in lead_intake.DUPLICATE_WEIGHTS.

    The breakdown behind the number is duplicate_breakdown(); this keeps
    the shape every existing caller reads.
    """
    d = duplicate_breakdown(msg=msg, subject=subject, from_addr=from_addr,
                            window_days=window_days)
    return d['score'], d['lead_id']


def duplicate_breakdown(msg=None, subject=None, from_addr=None,
                        window_days=30, limit=DUPLICATE_CANDIDATES):
    """{score, lead_id, reasons} for the lead this message most resembles.

    Every signal is compared against each recent lead and the lead with
    the highest total wins; `reasons` lists what fired for that lead as
    {signal, weight, detail}. What "the same" means for each signal is
    defined in _signals_for() and documented for administrators in
    docs/operations/CLASSIFICATION_GUIDE.md.

    Never raises: a scoring failure answers "no duplicate", which lets
    the rest of the tree decide rather than losing the message.
    """
    try:
        return _duplicate_breakdown(msg or {}, subject, from_addr,
                                    window_days, limit)
    except Exception:
        try:
            from app import app as flask_app
            flask_app.logger.exception('duplicate scoring failed')
        except Exception:
            pass
        return li.duplicate_details()


def _duplicate_breakdown(msg, subject, from_addr, window_days, limit):
    from app import Lead, db

    probe = _Probe(msg, subject, from_addr)
    since = datetime.utcnow() - timedelta(days=window_days)
    rows = (Lead.query
            .filter(Lead.email.isnot(None), Lead.created_at >= since)
            .order_by(Lead.created_at.desc()).limit(limit).all())

    # A thread match is decisive whatever the lead's age, so its lead is
    # considered even when it falls outside the window.
    thread_id, thread_detail = _thread_match(msg)
    if thread_id and thread_id not in {r.id for r in rows}:
        extra = db.session.get(Lead, thread_id)
        if extra is not None:
            rows.append(extra)
    if not rows:
        return li.duplicate_details()

    trail = _TrailIndex([r.id for r in rows], probe)

    best = li.duplicate_details()
    for lead in rows:
        reasons = _signals_for(lead, probe, trail,
                               thread_detail if lead.id == thread_id
                               else None)
        score = li.score_duplicate({r['signal']: True for r in reasons})
        if score > best['score']:
            reasons.sort(key=lambda r: -r['weight'])
            best = li.duplicate_details(score, lead.id, reasons)
    return best


class _Probe:
    """Everything about the incoming message the signals compare,
    worked out once rather than once per candidate lead."""

    def __init__(self, msg, subject, from_addr):
        from email_ingest import parser as email_parser

        self.msg = msg
        self.addr = (from_addr or '').strip().lower()
        self.domain = li.domain_of(self.addr)
        self.subject = _fold(subject or '')
        # find_by_subject refuses to match on fewer characters than this,
        # for the same reason: "RFQ" is every enquiry a customer sends.
        if len(self.subject) < 6:
            self.subject = ''

        self.text = li.searchable_text(msg)
        self.references = li.enquiry_references(self.text)
        self.files = {}
        for name in li.attachment_names(msg):
            key = li.meaningful_attachment(name)
            if key:
                self.files.setdefault(key, name)
        self.weights = li.cargo_weights(self.text)

        body = ''
        try:
            body = email_parser._get_body_text(msg)
        except Exception:
            body = li.body_text(msg)
        origin, destination = email_parser._extract_origin_destination(
            f"{msg.get('subject') or ''}\n{body}")
        self.route = _route_key(origin, destination)

        self.company = None
        if self.addr:
            try:
                self.company, _how = resolve_account(self.addr)
            except Exception:
                self.company = None
        self.personal = self.domain in _personal_domains()


class _TrailIndex:
    """What the candidate leads' trails hold, fetched in one query per
    kind and only when the message has something to compare it with."""

    def __init__(self, lead_ids, probe):
        from app import LeadAttachment, LeadEmail

        self.files = {}         # lead id → {normalised name: as stored}
        self.names = {}         # lead id → every filename, for references
        self.subjects = {}
        if not lead_ids:
            return
        if probe.files or probe.references:
            for lead_id, filename in (
                    LeadAttachment.query
                    .with_entities(LeadAttachment.lead_id,
                                   LeadAttachment.filename)
                    .filter(LeadAttachment.lead_id.in_(lead_ids)).all()):
                # Separators become spaces, exactly as searchable_text()
                # treats the message's own filenames, so a reference in
                # a filename reads the same on both sides.
                self.names.setdefault(lead_id, []).append(
                    re.sub(r'[_\-.]+', ' ', filename or ''))
                key = li.meaningful_attachment(filename)
                if key:
                    self.files.setdefault(lead_id, {}).setdefault(
                        key, filename)
        if probe.references:
            for lead_id, subject in (
                    LeadEmail.query
                    .with_entities(LeadEmail.lead_id, LeadEmail.subject)
                    .filter(LeadEmail.lead_id.in_(lead_ids),
                            LeadEmail.subject.isnot(None)).all()):
                self.subjects.setdefault(lead_id, []).append(subject)


def _signals_for(lead, probe, trail, thread_detail):
    """[{signal, weight, detail}] — which duplicate signals fire for one
    candidate lead.

    same_thread              the conversation id, In-Reply-To/References
                             or the message id itself names this lead or
                             an email on its trail
    same_reference           an enquiry reference (RFQ / tender / enquiry
                             number) appears in both; a short or all-digit
                             one only counts within the same account
    same_sender_and_subject  the sender is one of the lead's contact
                             addresses and the subject, with reply
                             prefixes and status notes removed, is the same
    same_account_and_route   same account, and the same origin AND
                             destination
    same_attachment_name     an attachment of the same name, ignoring
                             inline images, signature logos and names
                             made only of generic words ("RFQ.xlsx")
    same_account_in_window   same account, inside the time window
    same_cargo               every distinctive word of the lead's cargo
                             description appears in the message AND the
                             two state the same weight
    """
    W = li.DUPLICATE_WEIGHTS
    out = []

    def fire(signal, detail):
        out.append({'signal': signal, 'weight': W[signal], 'detail': detail})

    if thread_detail:
        fire('same_thread', thread_detail)

    lead_addrs = {a.strip().lower() for a in
                  (lead.email, lead.email2, lead.original_email_from) if a}
    lead_addrs.discard('')
    account = _same_account(lead, lead_addrs, probe)

    lead_text = '\n'.join(filter(None, [lead.original_email_subject,
                                        lead.original_email_body]))

    if probe.references:
        theirs = {key: raw for raw, key in li.enquiry_references(
            '\n'.join([lead_text] + trail.subjects.get(lead.id, [])
                      + trail.names.get(lead.id, [])))}
        for raw, key in probe.references:
            if key in theirs and (account or not li.weak_reference(key)):
                fire('same_reference', f'reference {raw} appears in both')
                break

    if probe.subject and probe.addr and probe.addr in lead_addrs:
        lead_subject = lead.original_email_subject or ''
        if probe.subject in (_fold(li.strip_prefixes(lead_subject)),
                             _fold(li.match_subject(lead_subject))):
            fire('same_sender_and_subject',
                 f'{probe.addr} sent the same subject before')

    extracted = _extracted(lead)
    if account and probe.route:
        theirs = _route_key(extracted.get('origin'),
                            extracted.get('destination'))
        if not theirs and lead_text:
            from email_ingest import parser as email_parser
            theirs = _route_key(*email_parser._extract_origin_destination(
                lead_text))
        if theirs and theirs == probe.route:
            fire('same_account_and_route',
                 f'same account and the same route '
                 f'({probe.route[0]} to {probe.route[1]})')

    if probe.files:
        shared = sorted(set(probe.files) & set(trail.files.get(lead.id, {})))
        if shared:
            fire('same_attachment_name',
                 f'attachment {probe.files[shared[0]]} is on the lead too')

    if account:
        fire('same_account_in_window', account)

    words = li.distinctive_cargo_words(extracted.get('cargo_type'))
    if words and probe.weights and li.mentions_all(probe.text, words):
        theirs = set()
        try:
            if extracted.get('cargo_weight_mt'):
                theirs.add(round(float(extracted['cargo_weight_mt']), 3))
        except (TypeError, ValueError):
            pass
        theirs |= li.cargo_weights(lead_text)
        same = sorted(theirs & probe.weights)
        if same:
            fire('same_cargo', f'same cargo ({" ".join(words)}) and the '
                               f'same weight ({same[0]:g} t)')
    return out


def _same_account(lead, lead_addrs, probe):
    """Words saying why the two share an account, or '' when they do not.

    The same address is the same customer; so is the same resolved
    account; so is the same company domain. A free-mail domain is not an
    account — two gmail.com senders are two strangers.
    """
    if probe.addr and probe.addr in lead_addrs:
        return f'same sender {probe.addr}'
    if probe.company is not None and lead.company_id \
            and lead.company_id == probe.company.id:
        return f'same account ({probe.company.name})'
    lead_domain = li.domain_of(lead.email or '')
    if probe.domain and not probe.personal and lead_domain == probe.domain:
        return f'same sender domain {probe.domain}'
    return ''


def _thread_match(msg):
    """(lead_id, detail) when the message's thread identity names a lead.

    The same lookups as find_by_thread(), plus the message id itself —
    the same email ingested twice — and each saying which one matched.
    """
    from app import Lead, LeadEmail

    keys = li.thread_keys(msg or {})
    conv = keys['conversation_id']
    if conv:
        row = Lead.query.with_entities(Lead.id).filter_by(
            conversation_id=conv).order_by(Lead.id.asc()).first()
        if row:
            return row[0], 'same conversation as the lead'
        mail = LeadEmail.query.with_entities(LeadEmail.lead_id).filter_by(
            conversation_id=conv).first()
        if mail:
            return mail[0], "same conversation as an email on the lead's trail"

    parents = [m for m in [keys['in_reply_to']] + list(keys['reference_ids'])
               if m]
    if parents:
        row = Lead.query.with_entities(Lead.id).filter(
            Lead.email_message_id.in_(parents)).first()
        if row:
            return row[0], "replies to the lead's original email"
        mail = LeadEmail.query.with_entities(LeadEmail.lead_id).filter(
            LeadEmail.message_id.in_(parents)).first()
        if mail:
            return mail[0], "replies to an email on the lead's trail"

    mid = keys['message_id']
    if mid:
        row = Lead.query.with_entities(Lead.id).filter(
            Lead.email_message_id == mid).first()
        if row:
            return row[0], 'this exact email already created the lead'
        mail = LeadEmail.query.with_entities(LeadEmail.lead_id).filter(
            LeadEmail.message_id == mid).first()
        if mail:
            return mail[0], "this exact email is already on the lead's trail"
    return None, None


def _route_key(origin, destination):
    o, d = li.place_key(origin), li.place_key(destination)
    return (o, d) if o and d else None


def _extracted(lead):
    import json
    try:
        data = json.loads(lead.email_extracted_json or '{}')
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _personal_domains():
    try:
        from email_ingest.parser import PERSONAL_DOMAINS
        return PERSONAL_DOMAINS
    except Exception:
        return set()


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
        duplicate_details=duplicate_breakdown,
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
            # The training dataset carries the evidence, not only the
            # verdict: which enquiry words were present, and which named
            # contributions produced the confidence score. Without them a
            # correction says the engine was wrong but not what misled it.
            'keywords': li.keywords(msg),
        }
        try:
            out['score_parts'] = [
                [name, n] for name, n in li.confidence_parts(
                    msg, sender_is_internal=False,
                    kind=li.subject_kind(msg.get('subject') or ''),
                    from_domain=li.domain_of(li.effective_sender(msg)))]
        except Exception:
            pass
        # Phase 4 — what the model said, and whether it was applied, kept
        # beside the rule's own answer so the two can be compared once
        # there is enough of both to judge.
        for key, value in ((decision.extra if decision else None) or {}).items():
            if key.startswith('ai_') or key == 'rule_class':
                out[key] = value
        # The duplicate breakdown — which signals fired against which
        # lead — so a reviewer overruling a duplicate can see what the
        # engine saw, and a correction records what misled it.
        dup = ((decision.extra if decision else None) or {}).get('duplicate')
        if dup:
            out['duplicate'] = dup
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
