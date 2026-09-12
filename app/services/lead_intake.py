"""
Lead intake classification — is this email a new enquiry, or noise?

The mailbox creates a lead per email that looks vaguely like logistics.
Employees keep leads@procamgroup.in in CC while corresponding with
customers and vendors, so replies, forwards, rate requests to shipping
lines and our own quotations all arrive there and all become leads.

This module decides what an email *is* before anything is created.

Shape
    classify(msg, ctx) -> Decision

    `msg` is the Graph message dict as the pipeline already has it.
    `ctx` is a Context of lookups the caller supplies — thread matching,
    duplicate scoring, vendor checks. Everything that touches the
    database lives behind that, so the decision tree itself is pure and
    can be tested against a dict.

Ordering is the design
    The cheap deterministic checks run before the expensive ambiguous
    ones, and the two rules that must never be overridden — a thread
    match and Account Master ownership — sit above anything scored. When
    a classification is wrong, the step that decided it is in the
    Decision, so it can be argued with.
"""
from __future__ import annotations

import re


# ─── the ten classes ─────────────────────────────────────────────────────
class Klass:
    NEW_LEAD      = 'A_new_lead'
    EXISTING      = 'B_existing_lead_comm'
    INTERNAL      = 'C_internal'
    DUPLICATE     = 'D_duplicate'
    REPLY         = 'E_reply'
    FORWARD       = 'F_forward_existing'
    RATE_SOURCING = 'G_rate_sourcing'
    QUOTE         = 'H_quote_submission'
    NON_BUSINESS  = 'I_non_business'
    REVIEW        = 'J_needs_review'

    #: The only class that creates a lead.
    CREATES_LEAD = (NEW_LEAD,)

    #: Classes that attach to an existing lead rather than standing alone.
    ATTACHES = (EXISTING, REPLY, FORWARD, DUPLICATE, RATE_SOURCING, QUOTE)

    LABELS = {
        NEW_LEAD:      'New Lead / RFQ',
        EXISTING:      'Existing Lead Communication',
        INTERNAL:      'Internal Communication',
        DUPLICATE:     'Duplicate',
        REPLY:         'Reply / Follow-up',
        FORWARD:       'Forward of Existing',
        RATE_SOURCING: 'Vendor / Rate Sourcing',
        QUOTE:         'Quote Submission',
        NON_BUSINESS:  'Non-business',
        REVIEW:        'Needs Admin Review',
    }


_REPLY_PREFIX = re.compile(
    r'^\s*(?:(?:re|aw|antw|sv|vs|res|odp)\s*(?:\[\d+\])?\s*:\s*)+', re.I)
_FWD_PREFIX = re.compile(
    r'^\s*(?:(?:fw|fwd|wg|tr|rv|enc)\s*(?:\[\d+\])?\s*:\s*)+', re.I)

#: A quotation being sent out, not an enquiry coming in.
_QUOTE_PHRASES = (
    'quotation attached', 'please find our quote', 'please find attached our quotation',
    'please find attached our quote', 'our offer', 'commercial offer',
    'quote submitted', 'proposal attached', 'find our offer',
    'as per your enquiry please find', 'we are pleased to quote',
    'our best offer', 'revised quotation', 'attached quotation',
    'quote as below', 'our quotation for',
)

#: Asking a vendor for a rate, rather than a customer asking us.
_RATE_REQUEST_PHRASES = (
    'please provide your best rate', 'please quote your best rate',
    'need trailer rate', 'please give ocean freight', 'request shipping line rate',
    'need transporter quote', 'kindly share your rate', 'share your best rate',
    'please share rates', 'requesting rates for', 'your lowest rate',
    'pls quote your best', 'need your best rate',
)

#: Words in a domain that mark a counterparty as a supplier, used only as
#: a hint — the learned vendor_domains table is the real source.
_VENDOR_DOMAIN_HINTS = (
    'shipping', 'liner', 'lines', 'maersk', 'msc', 'cma-cgm', 'hapag',
    'oocl', 'evergreen', 'cosco', 'transport', 'transporter', 'roadways',
    'carriers', 'logistics', 'freight', 'forwarder', 'cha', 'customs',
    'airlines', 'cargo', 'container', 'trailer', 'crane',
)


class Context:
    """Everything the tree needs to look up, supplied by the caller.

    Defaults make every lookup a miss, so the tree can be exercised
    without a database.
    """

    def __init__(self, *, internal_domains=(), find_by_thread=None,
                 find_by_subject=None, duplicate_score=None,
                 is_vendor_domain=None, has_logistics_content=None,
                 quote_reference=None):
        self.internal_domains = {d.lower().lstrip('@')
                                 for d in (internal_domains or ())}
        self.find_by_thread = find_by_thread or (lambda **kw: None)
        self.find_by_subject = find_by_subject or (lambda **kw: None)
        self.duplicate_score = duplicate_score or (lambda **kw: (0, None))
        self.is_vendor_domain = is_vendor_domain or (lambda d: False)
        self.has_logistics_content = has_logistics_content or (lambda m: True)
        self.quote_reference = quote_reference or (lambda text: None)


class Decision:
    """What the email is, why, and what to do about it."""

    def __init__(self, klass, *, step, reason, lead_id=None, confidence=None,
                 duplicate_score=0, needs_review=False, extra=None):
        self.klass = klass
        self.step = step                  # which rule decided
        self.reason = reason              # in words, for the audit log
        self.lead_id = lead_id            # what it attaches to
        self.confidence = confidence      # 0-100, None when deterministic
        self.duplicate_score = duplicate_score
        self.needs_review = needs_review
        self.extra = extra or {}

    @property
    def creates_lead(self):
        return self.klass in Klass.CREATES_LEAD

    @property
    def label(self):
        return Klass.LABELS.get(self.klass, self.klass)

    def to_dict(self):
        return {
            'classification': self.klass,
            'label': self.label,
            'step': self.step,
            'reason': self.reason,
            'lead_id': self.lead_id,
            'confidence': self.confidence,
            'duplicate_score': self.duplicate_score,
            'needs_review': self.needs_review,
            'creates_lead': self.creates_lead,
            **self.extra,
        }

    def __repr__(self):
        return (f'<Decision {self.klass} step={self.step} '
                f'lead={self.lead_id} conf={self.confidence}>')


# ─── small readers over the Graph message shape ──────────────────────────
def sender(msg):
    addr = ((msg.get('from') or {}).get('emailAddress') or {})
    return (addr.get('address') or '').strip().lower()


def recipients(msg, key='toRecipients'):
    out = []
    for r in (msg.get(key) or []):
        a = ((r or {}).get('emailAddress') or {}).get('address')
        if a:
            out.append(a.strip().lower())
    return out


def effective_sender(msg):
    """Who the email is really from.

    On a forward the parser has unwrapped, `from` is the Procam employee
    who relayed it and the customer is inside the body. Every judgement
    after that — is this internal, is it a vendor, is it a duplicate —
    has to be made about the customer, not the forwarder.
    """
    return (msg.get('_resolved_sender') or '').strip().lower() or sender(msg)


def domain_of(address):
    return (address or '').rsplit('@', 1)[-1].strip().lower() \
        if '@' in (address or '') else ''


def headers(msg):
    """RFC-822 headers as a lowercase dict.

    Graph returns internetMessageHeaders as a list of {name, value}, and
    only when it is asked for — which it was not until Phase 0.
    """
    out = {}
    for h in (msg.get('internetMessageHeaders') or []):
        name = (h or {}).get('name')
        if name:
            out[name.strip().lower()] = (h.get('value') or '').strip()
    return out


def thread_keys(msg):
    """Everything that identifies this message's conversation."""
    hdr = headers(msg)
    refs = hdr.get('references', '')
    return {
        'conversation_id': (msg.get('conversationId') or '').strip() or None,
        'in_reply_to': (hdr.get('in-reply-to') or '').strip() or None,
        'references': refs or None,
        'reference_ids': re.findall(r'<[^>]+>', refs) if refs else [],
        'message_id': (msg.get('internetMessageId') or '').strip() or None,
    }


def strip_prefixes(subject):
    """"RE: FW: RE: Transport requirement" → "Transport requirement"."""
    s = (subject or '').strip()
    for _ in range(6):          # people stack them
        before = s
        s = _REPLY_PREFIX.sub('', s)
        s = _FWD_PREFIX.sub('', s)
        if s == before:
            break
    return s.strip()


def subject_kind(subject):
    """'reply' | 'forward' | 'fresh' — from the prefix alone."""
    s = (subject or '').strip()
    if _REPLY_PREFIX.match(s):
        return 'reply'
    if _FWD_PREFIX.match(s):
        return 'forward'
    return 'fresh'


def body_text(msg):
    body = msg.get('body') or {}
    return (body.get('content') or msg.get('bodyPreview') or '')


def attachment_names(msg):
    return [((a or {}).get('name') or '') for a in (msg.get('attachments') or [])]


def searchable_text(msg):
    """Subject, body and attachment filenames together.

    A body of "Please find attached" with RFQ_Heavy_Transport.xlsx beside
    it is an RFQ; scanning only the prose would miss every enquiry sent
    the way most enquiries are actually sent.
    """
    # Filename separators become spaces: "RFQ_Heavy_Transport.xlsx" has no
    # word boundary after RFQ, so \brfq\b would never match it.
    files = ' '.join(re.sub(r'[_\-.]+', ' ', n) for n in attachment_names(msg))
    return '\n'.join(filter(None, [
        msg.get('subject') or '',
        body_text(msg),
        files,
    ]))


def _contains_any(text, phrases):
    low = (text or '').lower()
    for p in phrases:
        if p in low:
            return p
    return None


def looks_like_a_quotation(msg, ctx):
    """Us sending a price out, rather than a customer asking for one."""
    text = searchable_text(msg)
    phrase = _contains_any(text, _QUOTE_PHRASES)
    if phrase:
        return phrase
    ref = ctx.quote_reference(text)
    if ref:
        return f'quote reference {ref}'
    for att in (msg.get('attachments') or []):
        name = ((att or {}).get('name') or '').lower()
        if 'quot' in name or 'offer' in name:
            return f'attachment {name}'
    return None


def looks_like_a_rate_request(msg):
    return _contains_any(searchable_text(msg), _RATE_REQUEST_PHRASES)


def vendor_domain_hint(domain):
    """A weak signal only. Plenty of real customers are logistics firms,
    so this never decides on its own — it raises the question."""
    d = (domain or '').lower()
    return any(h in d for h in _VENDOR_DOMAIN_HINTS)


# ─── the tree ────────────────────────────────────────────────────────────
def classify(msg, ctx=None):
    """Decide what this email is. Never raises."""
    ctx = ctx or Context()
    subject = (msg.get('subject') or '').strip()
    resolved = bool(msg.get('_forward_resolved'))
    from_addr = effective_sender(msg)
    from_domain = domain_of(from_addr)
    keys = thread_keys(msg)
    kind = subject_kind(subject)
    stripped = strip_prefixes(subject)
    to_all = recipients(msg, 'toRecipients')
    cc_all = recipients(msg, 'ccRecipients')

    # A resolved forward is the customer's mail, relayed. Judging it by
    # the relayer's address makes every forwarded RFQ look internal.
    sender_is_internal = (from_domain in ctx.internal_domains
                          and not resolved)

    # ── 3. thread match — deterministic, final, outranks everything ──
    hit = ctx.find_by_thread(
        conversation_id=keys['conversation_id'],
        in_reply_to=keys['in_reply_to'],
        reference_ids=keys['reference_ids'])
    if hit:
        klass = (Klass.FORWARD if kind == 'forward'
                 else Klass.REPLY if kind == 'reply'
                 else Klass.EXISTING)
        return Decision(klass, step=3, lead_id=hit,
                        reason='thread matches an existing lead')

    # ── 4. our own people ──
    if sender_is_internal:
        quote = looks_like_a_quotation(msg, ctx)
        if quote:
            match = ctx.find_by_subject(subject=stripped,
                                        counterparties=to_all + cc_all)
            return Decision(Klass.QUOTE, step=4, lead_id=match,
                            reason=f'quotation sent out ({quote})',
                            needs_review=match is None,
                            extra={'quote_evidence': quote})

        if looks_like_a_rate_request(msg):
            match = ctx.find_by_subject(subject=stripped,
                                        counterparties=to_all + cc_all)
            return Decision(Klass.RATE_SOURCING, step=4, lead_id=match,
                            reason='rate request sent to a supplier')

        match = ctx.find_by_subject(subject=stripped,
                                    counterparties=to_all + cc_all)
        if match:
            return Decision(Klass.EXISTING, step=4, lead_id=match,
                            reason='our own correspondence on a known enquiry')
        return Decision(
            Klass.INTERNAL, step=4,
            reason='sent by a Procam address with the mailbox only copied in')

    # ── 5. reply / forward prefix with no thread header ──
    # A forward the parser unwrapped to an external sender is skipped
    # here: the FW: is how the mail reached us, not evidence of a thread
    # we already hold. Treating it as one sent 327 of 501 real messages
    # to Admin Review — every client RFQ an employee had relayed in.
    if kind in ('reply', 'forward') and not (resolved and kind == 'forward'):
        match = ctx.find_by_subject(
            subject=stripped, counterparties=[from_addr] + to_all + cc_all)
        if match:
            return Decision(
                Klass.REPLY if kind == 'reply' else Klass.FORWARD,
                step=5, lead_id=match,
                reason=f'{kind} matched on subject and counterparty')
        # No match. It is either a new enquiry inside a forward, or a
        # thread we never captured. A person can tell in seconds; a rule
        # cannot, and guessing wrong either loses an RFQ or invents one.
        return Decision(
            Klass.REVIEW, step=5, needs_review=True,
            reason=f'{kind} with no matching lead — could be a new enquiry')

    # ── 6. vendor ──
    if ctx.is_vendor_domain(from_domain):
        match = ctx.find_by_subject(subject=stripped,
                                    counterparties=to_all + cc_all)
        return Decision(Klass.RATE_SOURCING, step=6, lead_id=match,
                        reason=f'{from_domain} is a known supplier')

    # ── 7. an inbound quotation (an agent quoting us) ──
    quote = looks_like_a_quotation(msg, ctx)
    if quote:
        match = ctx.find_by_subject(subject=stripped,
                                    counterparties=[from_addr])
        if match:
            return Decision(Klass.QUOTE, step=7, lead_id=match,
                            reason=f'quotation against a known enquiry ({quote})')

    # ── 8. is it logistics at all ──
    if not ctx.has_logistics_content(msg):
        return Decision(Klass.NON_BUSINESS, step=8,
                        reason='no cargo, RFQ, route or contact signal')

    # ── 9. duplicate ──
    score, dup_of = ctx.duplicate_score(msg=msg, subject=stripped,
                                        from_addr=from_addr)
    if score >= 70:
        return Decision(Klass.DUPLICATE, step=9, lead_id=dup_of,
                        duplicate_score=score,
                        reason=f'duplicate of an existing enquiry ({score}%)')
    if score >= 31:
        return Decision(Klass.REVIEW, step=9, lead_id=dup_of,
                        duplicate_score=score, needs_review=True,
                        reason=f'may duplicate an existing enquiry ({score}%)')

    # ── 10. confidence ──
    confidence = lead_confidence(msg, ctx,
                                 sender_is_internal=sender_is_internal,
                                 kind=kind, from_domain=from_domain)
    if confidence < 50:
        return Decision(Klass.REVIEW, step=10, confidence=confidence,
                        duplicate_score=score, needs_review=True,
                        reason=f'low confidence ({confidence}%)')

    return Decision(Klass.NEW_LEAD, step=10, confidence=confidence,
                    duplicate_score=score,
                    needs_review=confidence < 80,
                    reason=f'new enquiry ({confidence}% confidence)')


# ─── confidence ──────────────────────────────────────────────────────────
def lead_confidence(msg, ctx=None, *, sender_is_internal=False, kind='fresh',
                    from_domain=''):
    """0–100. Extends the parser's score with the signals it cannot see.

    The parser's score at parser.py:940 only ever goes up, which is why a
    well-written reply from a real customer scores highly and becomes a
    lead. These weights can subtract.
    """
    ctx = ctx or Context()
    text = searchable_text(msg)
    low = text.lower()
    score = 40                      # a plausible email starts mid-scale

    # Positive — the customer is asking for something.
    if re.search(r'\b(rfq|rfp|rfi|tender|quotation|quote|enquiry|inquiry)\b', low):
        score += 25
    if re.search(r'\b(cargo|consignment|shipment|container|freight|cbm|'
                 r'tonnes?|mts?|odc|breakbulk|trailer|vessel|awb|bl)\b', low):
        score += 15
    if re.search(r'\b(from|ex|origin)\b.{0,40}\b(to|destination)\b', low):
        score += 10
    if re.search(r'(\+?\d[\d\s\-()]{8,})', text):
        score += 5
    if msg.get('hasAttachments') or attachment_names(msg):
        score += 5

    # Negative — the signals that make it not a new enquiry.
    if kind == 'reply':
        score -= 30
    elif kind == 'forward':
        score -= 10             # forwards are often genuine hand-offs
    if sender_is_internal:
        score -= 25
    if ctx.is_vendor_domain(from_domain) or vendor_domain_hint(from_domain):
        score -= 15
    if _contains_any(text, _QUOTE_PHRASES):
        score -= 30
    if _contains_any(text, _RATE_REQUEST_PHRASES):
        score -= 20
    if re.search(r'\b(unsubscribe|newsletter|webinar|no longer wish)\b', low):
        score -= 40

    return max(0, min(100, score))


# ─── duplicate scoring ───────────────────────────────────────────────────
#: Weights are configuration, not code — §06 of the design. Published here
#: so they can be tuned against the labelled sample rather than guessed at
#: twice.
DUPLICATE_WEIGHTS = {
    'same_thread': 100,
    'same_reference': 70,
    'same_sender_and_subject': 55,
    'same_account_and_route': 35,
    'same_attachment_name': 30,
    'same_account_in_window': 20,
    'same_cargo': 15,
}


def score_duplicate(signals):
    """Sum the weights of the signals that fired, capped at 100."""
    total = sum(DUPLICATE_WEIGHTS.get(k, 0)
                for k, fired in (signals or {}).items() if fired)
    return min(100, total)
