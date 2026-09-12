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
                 quote_reference=None, ai_opinion=None):
        self.internal_domains = {d.lower().lstrip('@')
                                 for d in (internal_domains or ())}
        self.find_by_thread = find_by_thread or (lambda **kw: None)
        self.find_by_subject = find_by_subject or (lambda **kw: None)
        self.duplicate_score = duplicate_score or (lambda **kw: (0, None))
        self.is_vendor_domain = is_vendor_domain or (lambda d: False)
        self.has_logistics_content = has_logistics_content or (lambda m: True)
        self.quote_reference = quote_reference or (lambda text: None)
        # Phase 4. Behind the Context like every other lookup, so the
        # tree stays pure and a model is never reached in a test that
        # did not ask for one.
        self.ai_opinion = ai_opinion


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
    # §20 — the requirement is often inside the attachment, not the body.
    # Supplied by the caller because reading a file is I/O and this
    # module stays pure; absent, the filenames still carry some signal.
    inside = msg.get('_attachment_text') or ''
    return '\n'.join(filter(None, [
        msg.get('subject') or '',
        body_text(msg),
        files,
        inside[:20000],
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
    if confidence < 25:
        # Not a borderline call. A newsletter scoring 2 or 12 does not
        # need a person to look at it — and it is parked, not deleted,
        # so a mistake here is recoverable.
        return Decision(Klass.NON_BUSINESS, step=10, confidence=confidence,
                        duplicate_score=score,
                        reason=f'no sign of an enquiry ({confidence}%)')
    if confidence < 50:
        decided = Decision(Klass.REVIEW, step=10, confidence=confidence,
                           duplicate_score=score, needs_review=True,
                           reason=f'low confidence ({confidence}%)')
    elif not has_substance(msg):
        # An email with nothing in it has told us nothing. The subject
        # can clear the bar on vocabulary alone: "Procam Group — India's
        # Integrated Logistics & Heavy-Lift Project Cargo Specialist"
        # scored 62 with an empty body — our own tagline, forwarded back
        # to us — and created a lead. There is no enquiry to be
        # confident about, so a person looks.
        decided = Decision(Klass.REVIEW, step=10, confidence=confidence,
                           duplicate_score=score, needs_review=True,
                           reason=f'nothing in the message to judge — '
                                  f'{confidence}% came from the subject '
                                  f'line alone')
    else:
        decided = Decision(Klass.NEW_LEAD, step=10, confidence=confidence,
                           duplicate_score=score,
                           needs_review=confidence < 80,
                           reason=f'new enquiry ({confidence}% confidence)')

    # Phase 4 — a second opinion, and only here. Everything above this
    # point is a fact: a thread match, a Procam sender, a known supplier.
    # A model has nothing to add to those and could only make them
    # wrong. It is reached last, on the uncertain band alone, and the
    # rule's own answer is kept beside whatever it says.
    if ctx.ai_opinion is not None:
        try:
            decided = ctx.ai_opinion(msg, decided) or decided
        except Exception:
            pass                    # the rule already has an answer
    return decided


# ─── confidence ──────────────────────────────────────────────────────────
def has_substance(msg):
    """Whether there is anything here to judge.

    Deliberately generous: "Pls quote" is nine characters and is a real
    enquiry, so anything at all in the body counts, and so does an
    attachment — most enquiries arrive as "please find attached". What
    this rejects is the genuinely empty message, where every point of
    the score came from words in the subject.
    """
    if (body_text(msg) or '').strip():
        return True
    if msg.get('hasAttachments') or attachment_names(msg):
        return True
    return bool((msg.get('_attachment_text') or '').strip())


def confidence_parts(msg, ctx=None, *, sender_is_internal=False,
                     kind='fresh', from_domain=''):
    """The score broken into named contributions.

    Returned rather than just summed so a threshold can be argued with:
    "this RFQ scored 45" is not actionable, "it scored 45 because a
    forward costs 10 and the body matched a quote phrase for 30" is.
    """
    ctx = ctx or Context()
    text = searchable_text(msg)
    low = text.lower()
    parts = [('base', 40)]

    if re.search(r'\b(rfq|rfp|rfi|tender|quotation|quote|enquiry|inquiry)\b', low):
        parts.append(('asks for a price', 25))
    if re.search(r'\b(cargo|consignment|shipment|container|freight|cbm|'
                 r'tonnes?|mts?|odc|breakbulk|trailer|vessel|awb|bl|'
                 r'transport|transportation|logistics|haulage|movement|'
                 r'clearance|warehous\w*|charter|rigging|axle)\b', low):
        parts.append(('logistics vocabulary', 15))
    if re.search(r'\b(from|ex|origin)\b.{0,40}\b(to|destination)\b', low) \
            or re.search(r'\b\w+\s*(?:–|-|→|to)\s*\w+\s*(?:port|icd|cfs)\b', low):
        parts.append(('a route', 10))
    if re.search(r'(\+?\d[\d\s\-()]{8,})', text):
        parts.append(('a phone number', 5))
    if msg.get('hasAttachments') or attachment_names(msg):
        parts.append(('an attachment', 5))
    if re.search(r'\b(rfq|enquiry|tender|quotation)[\s_\-/]*(no|number|ref|#)?'
                 r'[\s_\-/:]*[a-z0-9][a-z0-9\-/]{3,}', low):
        parts.append(('an enquiry reference', 10))

    if kind == 'reply':
        parts.append(('a reply', -30))
    elif kind == 'forward':
        # A forward is how most client mail reaches this mailbox, so it
        # is barely evidence against a new enquiry at all.
        parts.append(('a forward', -3))
    if sender_is_internal:
        parts.append(('sent by us', -25))
    if ctx.is_vendor_domain(from_domain) or vendor_domain_hint(from_domain):
        parts.append(('a supplier domain', -15))
    # Direction decides what these phrases mean. "our best offer" from
    # us is a quotation going out; from a customer it is them asking us
    # to quote, which is the definition of an enquiry. Penalising both
    # cost real RFQs 30 points each — "FOB Laem Chabang to ICD Dadri"
    # scored 27 on a body that was asking us for a price.
    if sender_is_internal:
        quote_phrase = _contains_any(text, _QUOTE_PHRASES)
        if quote_phrase:
            parts.append((f'we are quoting ({quote_phrase!r})', -30))
        rate_phrase = _contains_any(text, _RATE_REQUEST_PHRASES)
        if rate_phrase:
            parts.append((f'we are asking a supplier ({rate_phrase!r})', -20))
    else:
        # A customer using this wording is asking for a price.
        if _contains_any(text, _RATE_REQUEST_PHRASES):
            parts.append(('the customer is asking us to quote', +10))
    if re.search(r'\b(unsubscribe|newsletter|webinar|no longer wish)\b', low):
        parts.append(('newsletter wording', -40))

    return parts


def lead_confidence(msg, ctx=None, *, sender_is_internal=False, kind='fresh',
                    from_domain=''):
    """0-100. Extends the parser's score with the signals it cannot see.

    The parser's score at parser.py:940 only ever goes up, which is why a
    well-written reply from a real customer scores highly and becomes a
    lead. These weights can subtract.
    """
    total = sum(v for _name, v in confidence_parts(
        msg, ctx, sender_is_internal=sender_is_internal, kind=kind,
        from_domain=from_domain))
    return max(0, min(100, total))


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
