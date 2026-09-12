"""The intake decision tree — §03 of the design.

The mailbox creates a lead per email that looks like logistics. These
tests are the fifteen scenarios from the design's §11, plus the branches
they do not reach.

The classifier is pure — every database lookup is behind Context — so it
is loaded straight from its file and exercised against dicts.
"""
import importlib.util
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    'lead_intake_under_test',
    os.path.join(_ROOT, 'app', 'services', 'lead_intake.py'))
li = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(li)

K = li.Klass
PROCAM = 'procamgroup.in'


def msg(subject='', body='', frm='buyer@tatasteel.com', to=None, cc=None,
        conversation=None, in_reply_to=None, references=None,
        attachments=None, has_attachments=False, **extra):
    m = {
        'subject': subject,
        'body': {'content': body},
        'from': {'emailAddress': {'address': frm}},
        'toRecipients': [{'emailAddress': {'address': a}}
                         for a in (to or ['leads@procamgroup.in'])],
        'ccRecipients': [{'emailAddress': {'address': a}} for a in (cc or [])],
        'hasAttachments': has_attachments,
        'attachments': attachments or [],
    }
    if conversation:
        m['conversationId'] = conversation
    hdrs = []
    if in_reply_to:
        hdrs.append({'name': 'In-Reply-To', 'value': in_reply_to})
    if references:
        hdrs.append({'name': 'References', 'value': references})
    if hdrs:
        m['internetMessageHeaders'] = hdrs
    m.update(extra)
    return m


def ctx(**kw):
    kw.setdefault('internal_domains', [PROCAM])
    return li.Context(**kw)


RFQ_BODY = ("Dear Procam, we have a requirement to move a 220 MT transformer "
            "from JNPT to Vadodara. Over-dimensional cargo. Please quote by "
            "Friday. Contact +91 98200 11223.")


# ─── subject parsing, which everything downstream leans on ───────────────
@pytest.mark.parametrize('subject,expected', [
    ('RFQ - transformer movement', 'fresh'),
    ('RE: RFQ - transformer movement', 'reply'),
    ('Re: RFQ', 'reply'),
    ('RE: RE: RE: RFQ', 'reply'),
    ('FW: RFQ', 'forward'),
    ('Fwd: RFQ', 'forward'),
    ('FWD: RFQ', 'forward'),
    ('AW: RFQ', 'reply'),        # German auto-prefix
    ('RE[2]: RFQ', 'reply'),
    ('Reference our RFQ', 'fresh'),   # must not fire on a word starting "re"
])
def test_the_prefix_is_read_correctly(subject, expected):
    assert li.subject_kind(subject) == expected


@pytest.mark.parametrize('subject,expected', [
    ('RE: RFQ - transformer', 'RFQ - transformer'),
    ('FW: RE: FW: RFQ - transformer', 'RFQ - transformer'),
    ('RFQ - transformer', 'RFQ - transformer'),
])
def test_prefixes_are_stripped_for_matching(subject, expected):
    assert li.strip_prefixes(subject) == expected


def test_thread_headers_are_read_from_graphs_shape():
    m = msg(conversation='AAQk123', in_reply_to='<a@x.com>',
            references='<a@x.com> <b@x.com>')
    keys = li.thread_keys(m)
    assert keys['conversation_id'] == 'AAQk123'
    assert keys['in_reply_to'] == '<a@x.com>'
    assert keys['reference_ids'] == ['<a@x.com>', '<b@x.com>']


# ─── §11 scenarios ───────────────────────────────────────────────────────
def test_a_fresh_rfq_from_a_client_creates_a_lead():
    d = li.classify(msg(subject='RFQ - Heavy transport requirement',
                        body=RFQ_BODY), ctx())
    assert d.klass == K.NEW_LEAD
    assert d.creates_lead
    assert d.confidence >= 80


def test_a_thread_match_wins_over_everything():
    """Deterministic and final — no score, no model."""
    d = li.classify(
        msg(subject='RFQ - Heavy transport requirement', body=RFQ_BODY,
            conversation='AAQk123'),
        ctx(find_by_thread=lambda **kw: 4242))
    assert d.klass == K.EXISTING
    assert d.lead_id == 4242
    assert d.step == 3
    assert not d.creates_lead


def test_a_rewritten_subject_cannot_defeat_the_headers():
    """Headers win over subject text — the whole point of Phase 0."""
    d = li.classify(
        msg(subject='Completely different subject line',
            in_reply_to='<original@tatasteel.com>'),
        ctx(find_by_thread=lambda **kw: 77))
    assert d.klass == K.EXISTING
    assert d.lead_id == 77


def test_a_reply_on_a_known_subject_appends():
    d = li.classify(msg(subject='RE: RFQ - transformer', body=RFQ_BODY),
                    ctx(find_by_subject=lambda **kw: 9))
    assert d.klass == K.REPLY
    assert d.lead_id == 9
    assert not d.creates_lead


def test_a_reply_with_no_match_goes_to_review_not_to_a_lead():
    d = li.classify(msg(subject='RE: RFQ - transformer', body=RFQ_BODY), ctx())
    assert d.klass == K.REVIEW
    assert d.needs_review
    assert not d.creates_lead


def test_a_forward_with_no_match_goes_to_review():
    """Could be a new enquiry inside, or a thread we lost. A person can
    tell in seconds; guessing loses an RFQ or invents one."""
    d = li.classify(msg(subject='FW: RFQ - new requirement', body=RFQ_BODY),
                    ctx())
    assert d.klass == K.REVIEW
    assert d.needs_review


def test_an_employee_forward_of_a_client_rfq_becomes_a_lead():
    """The parser has already promoted the external sender; the tree must
    not then treat it as internal correspondence."""
    d = li.classify(
        msg(subject='FW: RFQ - Heavy transport', body=RFQ_BODY,
            frm='buyer@tatasteel.com', _forward_resolved=True),
        ctx(find_by_thread=lambda **kw: None))
    assert d.klass in (K.NEW_LEAD, K.REVIEW)
    assert d.klass != K.INTERNAL


def test_an_employee_ccing_the_mailbox_never_creates_a_lead():
    d = li.classify(
        msg(subject='Transformer movement update', body=RFQ_BODY,
            frm='sales@procamgroup.in', to=['buyer@tatasteel.com'],
            cc=['leads@procamgroup.in']),
        ctx())
    assert d.klass in (K.INTERNAL, K.EXISTING)
    assert not d.creates_lead


def test_an_employee_ccing_on_a_known_enquiry_appends_to_it():
    d = li.classify(
        msg(subject='Transformer movement update', body=RFQ_BODY,
            frm='sales@procamgroup.in', to=['buyer@tatasteel.com']),
        ctx(find_by_subject=lambda **kw: 55))
    assert d.klass == K.EXISTING
    assert d.lead_id == 55


def test_a_quotation_we_sent_moves_the_lead_to_quoted():
    d = li.classify(
        msg(subject='Our quotation - transformer movement',
            body='Dear sir, please find attached our quotation for the '
                 'transformer movement. Rate valid 30 days.',
            frm='sales@procamgroup.in', to=['buyer@tatasteel.com']),
        ctx(find_by_subject=lambda **kw: 31))
    assert d.klass == K.QUOTE
    assert d.lead_id == 31
    assert not d.creates_lead


def test_a_quotation_attachment_is_enough_on_its_own():
    d = li.classify(
        msg(subject='Transformer movement', body='As discussed.',
            frm='sales@procamgroup.in', to=['buyer@tatasteel.com'],
            attachments=[{'name': 'Procam_Quotation_4471.pdf'}]),
        ctx(find_by_subject=lambda **kw: 12))
    assert d.klass == K.QUOTE


def test_a_rate_request_to_a_supplier_is_not_a_lead():
    d = li.classify(
        msg(subject='Rate required Mundra to Chennai',
            body='Please provide your best rate for a 40ft trailer.',
            frm='ops@procamgroup.in', to=['sales@sometransport.com']),
        ctx())
    assert d.klass == K.RATE_SOURCING
    assert not d.creates_lead


def test_a_known_vendor_writing_in_is_rate_sourcing():
    d = li.classify(
        msg(subject='Our rates for your enquiry', body=RFQ_BODY,
            frm='sales@xyzshippingline.com'),
        ctx(is_vendor_domain=lambda d_: d_ == 'xyzshippingline.com',
            find_by_subject=lambda **kw: 88))
    assert d.klass == K.RATE_SOURCING
    assert d.lead_id == 88


def test_a_duplicate_appends_instead_of_creating():
    d = li.classify(msg(subject='RFQ - transformer', body=RFQ_BODY),
                    ctx(duplicate_score=lambda **kw: (85, 501)))
    assert d.klass == K.DUPLICATE
    assert d.lead_id == 501
    assert d.duplicate_score == 85


def test_an_uncertain_duplicate_goes_to_review():
    d = li.classify(msg(subject='RFQ - transformer', body=RFQ_BODY),
                    ctx(duplicate_score=lambda **kw: (45, 501)))
    assert d.klass == K.REVIEW
    assert d.needs_review


def test_a_genuine_second_enquiry_is_not_blocked():
    """Today's 30-day same-domain rule blocks this. It must not."""
    d = li.classify(
        msg(subject='RFQ - second consignment, Kandla to Jamnagar',
            body='New requirement, 3 reactors, 180 MT each. Please quote.'),
        ctx(duplicate_score=lambda **kw: (20, None)))
    assert d.klass == K.NEW_LEAD
    assert d.creates_lead


def test_an_attachment_carries_the_signal_when_the_body_does_not():
    d = li.classify(
        msg(subject='Requirement', body='Please find attached.',
            has_attachments=True,
            attachments=[{'name': 'RFQ_Heavy_Transport.xlsx'}]),
        ctx())
    assert d.klass == K.NEW_LEAD


def test_marketing_mail_is_not_a_lead():
    d = li.classify(
        msg(subject='Webinar: the future of freight',
            body='Join our webinar. Unsubscribe here.',
            frm='news@logisticssaas.com'),
        ctx(has_logistics_content=lambda m: False))
    assert d.klass == K.NON_BUSINESS


def test_nothing_but_class_a_ever_creates_a_lead():
    for klass in (K.EXISTING, K.INTERNAL, K.DUPLICATE, K.REPLY, K.FORWARD,
                  K.RATE_SOURCING, K.QUOTE, K.NON_BUSINESS, K.REVIEW):
        assert klass not in K.CREATES_LEAD


# ─── confidence ──────────────────────────────────────────────────────────
def test_a_reply_scores_below_a_fresh_enquiry():
    fresh = li.lead_confidence(msg(subject='RFQ - transformer', body=RFQ_BODY),
                               ctx(), kind='fresh')
    reply = li.lead_confidence(msg(subject='RE: RFQ - transformer',
                                   body=RFQ_BODY), ctx(), kind='reply')
    assert reply < fresh - 20, 'a reply must not score like a new enquiry'


def test_the_score_can_go_down():
    """The parser's score only ever adds, which is the bug this fixes."""
    plain = li.lead_confidence(msg(subject='RFQ', body=RFQ_BODY), ctx())
    ours = li.lead_confidence(msg(subject='RFQ', body=RFQ_BODY), ctx(),
                              sender_is_internal=True)
    assert ours < plain


def test_a_quotation_scores_far_below_an_enquiry():
    enquiry = li.lead_confidence(msg(subject='RFQ', body=RFQ_BODY), ctx())
    quote = li.lead_confidence(
        msg(subject='RFQ', body='Please find attached our quotation.'), ctx())
    assert quote < enquiry


@pytest.mark.parametrize('score', [0, 100])
def test_confidence_stays_in_range(score):
    v = li.lead_confidence(msg(subject='x' * 200, body='y' * 500), ctx())
    assert 0 <= v <= 100


# ─── duplicate weights ───────────────────────────────────────────────────
def test_a_thread_match_alone_is_decisive():
    assert li.score_duplicate({'same_thread': True}) == 100


def test_weights_accumulate_and_cap():
    assert li.score_duplicate({'same_sender_and_subject': True}) == 55
    assert li.score_duplicate({'same_sender_and_subject': True,
                               'same_attachment_name': True}) == 85
    assert li.score_duplicate({'same_thread': True,
                               'same_reference': True}) == 100


def test_no_signals_is_not_a_duplicate():
    assert li.score_duplicate({}) == 0
    assert li.score_duplicate(None) == 0


# ─── robustness ──────────────────────────────────────────────────────────
@pytest.mark.parametrize('bad', [{}, {'subject': None}, {'from': None},
                                 {'body': None}, {'toRecipients': None}])
def test_a_malformed_message_does_not_crash_the_tree(bad):
    d = li.classify(bad, ctx())
    assert d.klass in li.Klass.LABELS


def test_every_decision_explains_itself():
    """A classification nobody can argue with is a classification nobody
    can fix."""
    d = li.classify(msg(subject='RFQ - transformer', body=RFQ_BODY), ctx())
    assert d.step and d.reason
    assert d.to_dict()['label']


# ─── the forward flag decides 90% of this mailbox ────────────────────────
def test_an_unwrapped_forward_is_not_treated_as_internal():
    """The mailbox is fed largely by employees forwarding client mail.

    Without the parser's forward_resolved flag every one of those looks
    like it came from the Procam employee who relayed it, and lands as
    Internal. A dry run over 501 real messages reported 71% Internal for
    exactly that reason.
    """
    forwarded = msg(subject='FW: RFQ - Heavy transport requirement',
                    body=RFQ_BODY, frm='buyer@tatasteel.com',
                    _forward_resolved=True)
    d = li.classify(forwarded, ctx())
    assert d.klass != K.INTERNAL

    # The same message without the flag — the bug — goes elsewhere.
    unflagged = dict(forwarded)
    unflagged.pop('_forward_resolved')
    assert li.classify(unflagged, ctx()).klass != K.NEW_LEAD


def test_the_procam_branch_only_fires_on_a_procam_sender():
    """An external sender must never reach step 4, however the mailbox
    is addressed."""
    d = li.classify(
        msg(subject='RFQ - transformer', body=RFQ_BODY,
            frm='buyer@tatasteel.com',
            to=['sales@procamgroup.in'], cc=['leads@procamgroup.in']),
        ctx())
    assert d.step != 4
    assert d.klass == K.NEW_LEAD


# ─── a relayed client enquiry is the client's, not the relayer's ─────────
def test_a_relayed_client_rfq_is_judged_by_the_client():
    """342 of 501 real messages are forwards that unwrap to an external
    sender. Judging them by the forwarder's address made every one look
    internal; catching them on the FW: prefix afterwards sent 327 to
    Admin Review. Both were wrong: this is the customer's enquiry.
    """
    d = li.classify(
        msg(subject='FW: RFQ - Heavy transport requirement', body=RFQ_BODY,
            frm='sales@procamgroup.in',
            _forward_resolved=True,
            _resolved_sender='buyer@tatasteel.com'),
        ctx())
    assert d.klass == K.NEW_LEAD, f'got {d.klass} at step {d.step}'
    assert d.creates_lead


def test_the_resolved_sender_is_used_for_every_later_judgement():
    """A relayed enquiry from a shipping line is still rate sourcing —
    the check has to run on the customer, not the colleague."""
    d = li.classify(
        msg(subject='FW: Our rates', body=RFQ_BODY,
            frm='sales@procamgroup.in', _forward_resolved=True,
            _resolved_sender='quotes@xyzshippingline.com'),
        ctx(is_vendor_domain=lambda d_: d_ == 'xyzshippingline.com'))
    assert d.klass == K.RATE_SOURCING


def test_an_unresolved_forward_from_us_is_still_internal():
    """Only an unwrapped forward gets the benefit of the doubt."""
    d = li.classify(
        msg(subject='FW: some thread', body='see below',
            frm='sales@procamgroup.in'),
        ctx())
    assert d.klass in (K.INTERNAL, K.EXISTING)
    assert not d.creates_lead


def test_a_relayed_reply_is_still_a_reply():
    """RE: inside a forward is still correspondence, not a new enquiry."""
    d = li.classify(
        msg(subject='RE: RFQ - transformer', body=RFQ_BODY,
            frm='buyer@tatasteel.com'),
        ctx())
    assert d.klass in (K.REPLY, K.REVIEW)
    assert not d.creates_lead


# ─── calibration against the real mailbox ────────────────────────────────
REAL_SUBJECTS = [
    'RFQ-DLI-26-0056 | Road Transportation – Nhava Sheva to Pune',
    'RFQ-DLI-26-0054 – ODC Transportation by Axle Pullers',
    'Invitation for Supply of MHE W/o Operator- In-Plant',
    'RFQ for India shipment (CGTR)',
    'Quotation// Enquiry TMILL/0095/2026-27',
    'FOB Laem Chabang to ICD Dadri / HMC Polymer',
    'REQUEST FOR PRICE DETAILS - FCA - Kotka 48600, Finland',
]


@pytest.mark.parametrize('subject', REAL_SUBJECTS)
def test_real_client_enquiries_clear_the_confidence_floor(subject):
    """Subjects taken verbatim from the live mailbox, all of which the
    first calibration run sent to Admin Review.

    They arrive forwarded by a colleague, which is how nearly every
    client enquiry reaches this mailbox — so a forward can only be weak
    evidence against a new enquiry, not strong.

    Caveat worth keeping in view: the subjects are real, the bodies are
    invented. This pins the mechanism, not the calibration. Only a dry
    run over the actual mailbox can confirm the threshold, and these
    tests must not be mistaken for that.
    """
    m = msg(subject='Fw: ' + subject,
            body='Please find the enquiry below. Kindly quote.',
            frm='sales@procamgroup.in',
            _forward_resolved=True,
            _resolved_sender='buyer@clientcompany.com')
    score = li.lead_confidence(m, ctx(), kind='forward')
    assert score >= 50, (
        f'{subject!r} scored {score}: '
        + '  '.join(f'{n} {v:+d}'
                    for n, v in li.confidence_parts(m, ctx(), kind='forward')))


def test_the_score_can_be_explained():
    """"It scored 45" is not actionable; the breakdown is."""
    parts = li.confidence_parts(
        msg(subject='RFQ - transformer', body=RFQ_BODY), ctx())
    names = [n for n, _v in parts]
    assert 'base' in names
    assert any('price' in n for n in names)
    raw = sum(v for _n, v in parts)
    # lead_confidence clamps to 0-100; the parts are the unclamped truth.
    assert li.lead_confidence(
        msg(subject='RFQ - transformer', body=RFQ_BODY), ctx()) \
        == max(0, min(100, raw))


def test_a_forwarded_newsletter_is_not_sent_to_a_human():
    """Five of the first twenty-five review items were forwarded
    newsletters."""
    d = li.classify(
        msg(subject='Fw: SCC Online Newsletter Vol.14 Issue 773',
            body='This week in law. Unsubscribe here.',
            frm='sales@procamgroup.in', _forward_resolved=True,
            _resolved_sender='newsletter@scconline.com'),
        ctx(has_logistics_content=lambda m: False))
    assert d.klass == K.NON_BUSINESS
    assert not d.needs_review


# ─── direction decides what the wording means ────────────────────────────
def test_a_customer_asking_for_our_best_rate_is_an_enquiry():
    """Verbatim from the mailbox: "Inquiry / Project 41010447_OCP" scored
    42 because the customer's own words — "our best offer", "please
    provide your best rate" — were read as us quoting a supplier. That is
    the phrasing of an RFQ, not of a quotation.
    """
    m = msg(subject='Fw: Inquiry / Project 41010447+448_OCP / Structural steel',
            body='Dear Procam, please provide your best rate for the '
                 'movement from Antwerp to Nhava Sheva. Awaiting our best '
                 'offer by Friday.',
            frm='sales@procamgroup.in', _forward_resolved=True,
            _resolved_sender='mg@g-p-solutions.de')
    d = li.classify(m, ctx())
    assert d.klass == K.NEW_LEAD, (
        f'{d.klass} at step {d.step}: '
        + '  '.join(f'{n} {v:+d}' for n, v in
                    li.confidence_parts(m, ctx(), kind='forward')))


def test_the_same_wording_from_us_is_still_a_quotation():
    """The rule is direction, not vocabulary."""
    d = li.classify(
        msg(subject='Transformer movement',
            body='Dear sir, please find attached our quotation.',
            frm='sales@procamgroup.in', to=['buyer@tatasteel.com']),
        ctx(find_by_subject=lambda **kw: 7))
    assert d.klass == K.QUOTE


def test_a_customer_asking_a_supplier_question_is_not_rate_sourcing():
    d = li.classify(
        msg(subject='Fw: FOB Laem Chabang to ICD Dadri',
            body='Kindly share your rate for this movement. '
                 'Awaiting our offer.',
            frm='sales@procamgroup.in', _forward_resolved=True,
            _resolved_sender='bharat.kumar@uflexltd.com'),
        ctx())
    assert d.klass != K.RATE_SOURCING
    assert d.creates_lead or d.needs_review


def test_an_obvious_newsletter_does_not_cost_a_human_a_look():
    """Scoring 2 is not a borderline call. It is parked, not deleted."""
    m = msg(subject='Fw: Dubai Jumeirah Beach Luxury w. Daily Breakfast',
            body='Book now. Unsubscribe here.',
            frm='sales@procamgroup.in', _forward_resolved=True,
            _resolved_sender='email@m.luxuryescapes.com')
    d = li.classify(m, ctx())
    assert d.klass == K.NON_BUSINESS
    assert not d.needs_review


def test_a_borderline_message_still_reaches_a_person():
    """The floor must not swallow the genuinely uncertain."""
    m = msg(subject='Fw: Requirement', body='Please advise on the below.',
            frm='sales@procamgroup.in', _forward_resolved=True,
            _resolved_sender='someone@clientco.com')
    d = li.classify(m, ctx())
    assert 25 <= (d.confidence or 0) < 50
    assert d.klass == K.REVIEW


# ─── an empty message is not an enquiry ──────────────────────────────────
#
# Lead 11670: a customer forwarded our own company profile back to us.
# Empty body, and the subject was our tagline. Base 40 plus 'logistics
# vocabulary' 15 cleared the 50 threshold and a lead was created from a
# message that said nothing.
_OUR_OWN_TAGLINE = ("Fw: Procam Group – India's Integrated Logistics & "
                    "Heavy-Lift Project Cargo Specialist")


def _the_real_one(**over):
    """Lead 11670 as it actually arrived.

    _forward_resolved is True with an empty _resolved_sender: the parser
    reported it had unwrapped the forward while producing no sender and
    no body. That is what let it past step 5 and into the score.
    """
    m = msg(subject=_OUR_OWN_TAGLINE, body='',
            frm='sanjay.singh15@motherson.com')
    m['_forward_resolved'] = True
    m.update(over)
    return m


def test_an_empty_body_does_not_become_a_lead_on_subject_words_alone():
    d = li.classify(_the_real_one(), li.Context())
    assert d.klass == K.REVIEW
    assert d.needs_review is True
    assert 'nothing in the message' in d.reason


def test_the_score_is_still_reported_so_the_call_can_be_argued_with():
    """It did clear the bar. Hiding that would make the rule unarguable."""
    d = li.classify(_the_real_one(), li.Context())
    assert d.confidence >= 50


def test_please_find_attached_is_still_a_lead():
    """The gate must not catch the way most real enquiries arrive."""
    d = li.classify(
        msg(subject='RFQ for breakbulk movement Airoli to Kandla', body='',
            attachments=[{'name': 'RFQ_Heavy_Transport.xlsx'}]),
        li.Context())
    assert d.klass == K.NEW_LEAD


def test_nine_characters_of_body_is_enough():
    """"Pls quote" is a real enquiry. The gate rejects empty, not short."""
    d = li.classify(msg(subject='RFQ breakbulk cargo movement Airoli',
                        body='Pls quote'), li.Context())
    assert d.klass == K.NEW_LEAD


def test_whitespace_is_not_content():
    assert li.has_substance(msg(subject='x', body='   \n\n \t ')) is False


def test_a_body_preview_counts_as_content():
    """Graph gives a preview where the full body was not fetched."""
    m = msg(subject='x', body='')
    m['bodyPreview'] = 'We need a quote for 40 MT to Kandla'
    assert li.has_substance(m) is True


def test_attachment_text_counts_as_content():
    m = msg(subject='x', body='')
    m['_attachment_text'] = 'Origin Airoli Destination Kandla 40 MT'
    assert li.has_substance(m) is True


def test_the_gate_does_not_touch_deterministic_answers():
    """A thread match is a fact. An empty reply on a known thread still
    belongs to its lead — the gate sits at step 10 and nowhere else."""
    ctx = li.Context(find_by_thread=lambda **kw: 42)
    d = li.classify(_the_real_one(), ctx)
    assert d.klass == K.FORWARD
    assert d.lead_id == 42
    assert d.step != 10


def test_an_empty_message_that_scores_low_is_still_parked_not_reviewed():
    """The gate raises the bar for creating a lead. It does not drag
    obvious noise into the queue for a person to read."""
    d = li.classify(msg(subject='Unsubscribe from our newsletter', body='',
                        frm='news@example.com'), li.Context())
    assert d.klass == K.NON_BUSINESS
