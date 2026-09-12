"""Phase 4 — the model, and everything it is not allowed to do.

The point of these is not that the classifier is clever. It is that a
model cannot reach past the deterministic rules, cannot break intake
when a third party is slow, and cannot quietly replace a decision
without leaving the rule's own answer beside it.

Nothing here makes a network call.
"""
import importlib.util
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def _pure(name, path):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_ROOT, path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


li = _pure('li_ai_test', 'app/services/lead_intake.py')
# lead_intake_ai imports lead_intake by package path; point it at ours so
# the Klass identities match.
sys.modules['app.services.lead_intake'] = li
ai = _pure('ai_test', 'app/services/lead_intake_ai.py')

K = li.Klass
PROCAM = 'procamgroup.in'
RFQ_BODY = ('Dear Procam, we need to move a 220 MT transformer from JNPT '
            'to Vadodara. Please quote.')


def msg(subject='', body='', frm='buyer@tatasteel.com', **extra):
    m = {'subject': subject, 'body': {'content': body},
         'from': {'emailAddress': {'address': frm}},
         'toRecipients': [{'emailAddress': {'address': 'leads@procamgroup.in'}}],
         'ccRecipients': []}
    m.update(extra)
    return m


def decision(klass=K.REVIEW, step=10, confidence=45):
    return li.Decision(klass, step=step, confidence=confidence,
                       reason='low confidence (45%)',
                       needs_review=(klass == K.REVIEW))


# ─── it stays off unless switched on ─────────────────────────────────────
def test_it_is_off_by_default(monkeypatch):
    monkeypatch.delenv('LEAD_INTAKE_AI', raising=False)
    monkeypatch.setenv('GROQ_API_KEY', 'x')
    assert ai.is_enabled() is False


def test_the_flag_alone_is_not_enough(monkeypatch):
    """The flag is the business decision; the key is whether it can work."""
    monkeypatch.setenv('LEAD_INTAKE_AI', 'on')
    monkeypatch.delenv('GROQ_API_KEY', raising=False)
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    assert ai.is_enabled() is False


def test_both_together_enable_it(monkeypatch):
    monkeypatch.setenv('LEAD_INTAKE_AI', 'on')
    monkeypatch.setenv('GROQ_API_KEY', 'x')
    assert ai.is_enabled() is True


# ─── it is never asked about a deterministic decision ────────────────────
@pytest.mark.parametrize('step', [3, 4, 5, 6, 7, 8, 9])
def test_a_deterministic_decision_is_never_second_guessed(step):
    """A thread match is a fact. A model has nothing to add to it and
    could only make it wrong."""
    d = li.Decision(K.EXISTING, step=step, reason='thread match', lead_id=7)
    assert ai.should_consult(d) is False


def test_a_confident_rule_is_not_worth_a_call():
    assert ai.should_consult(decision(K.NEW_LEAD, confidence=97)) is False


def test_an_obviously_worthless_email_is_not_worth_a_call():
    assert ai.should_consult(decision(K.NON_BUSINESS, confidence=8)) is False


def test_only_the_uncertain_band_is_consulted():
    assert ai.should_consult(decision(confidence=45)) is True
    assert ai.should_consult(decision(confidence=79)) is True
    assert ai.should_consult(decision(confidence=80)) is False
    assert ai.should_consult(decision(confidence=24)) is False


def test_the_tree_does_not_reach_a_model_unless_one_is_supplied():
    """A Context with no ai_opinion must behave exactly as before."""
    ctx = li.Context(internal_domains=[PROCAM])
    assert ctx.ai_opinion is None
    d = li.classify(msg(subject='RFQ - transformer', body=RFQ_BODY), ctx)
    assert d.klass == K.NEW_LEAD


def test_the_model_is_only_consulted_after_every_rule(monkeypatch):
    seen = []

    def spy(m, decided):
        seen.append(decided.step)
        return decided

    ctx = li.Context(internal_domains=[PROCAM], ai_opinion=spy,
                     find_by_thread=lambda **kw: 42)
    li.classify(msg(subject='RE: RFQ', body=RFQ_BODY), ctx)
    assert seen == [], 'a thread match must return before the model'


# ─── what an opinion may and may not do ──────────────────────────────────
def test_a_confident_opinion_moves_an_uncertain_decision():
    d = ai.apply(decision(K.REVIEW, confidence=45),
                 ai.Opinion(K.NEW_LEAD, 88, 'a customer asking for a price'))
    assert d.klass == K.NEW_LEAD
    assert d.extra['ai_applied'] is True
    assert d.extra['rule_class'] == K.REVIEW, \
        "the rule's own answer must survive beside the model's"


def test_an_unsure_model_does_not_override():
    """A model less certain than the score it second-guesses has not
    earned the override."""
    d = ai.apply(decision(K.REVIEW, confidence=45),
                 ai.Opinion(K.NEW_LEAD, 55, 'might be'))
    assert d.klass == K.REVIEW
    assert d.extra['ai_applied'] is False
    assert d.extra['ai_class'] == K.NEW_LEAD, 'but it is still recorded'


def test_no_opinion_leaves_the_rule_alone():
    original = decision(K.REVIEW, confidence=45)
    assert ai.apply(original, None) is original


def test_an_opinion_cannot_touch_a_deterministic_decision():
    d = li.Decision(K.EXISTING, step=3, reason='thread match', lead_id=9)
    out = ai.apply(d, ai.Opinion(K.NEW_LEAD, 99, 'looks like an RFQ'))
    assert out.klass == K.EXISTING
    assert out.lead_id == 9


def test_an_opinion_cannot_invent_a_lead_to_attach_to():
    d = ai.apply(decision(K.REVIEW, confidence=45),
                 ai.Opinion(K.EXISTING, 95, 'part of a thread'))
    # EXISTING is not in the allowed set, so it never becomes an Opinion
    # in the first place — but if one is constructed by hand it still
    # cannot supply a lead_id.
    assert d.lead_id is None


# ─── nothing the model returns is trusted ────────────────────────────────
@pytest.mark.parametrize('raw', [
    '', 'not json at all', '{}', '[]', 'null',
    '{"classification": "something_else", "confidence": 90}',
    '{"classification": "new_lead"',
    '{"confidence": 90}',
])
def test_junk_from_the_model_yields_nothing(raw):
    assert ai._parse(raw) is None


def test_a_class_outside_the_allowed_set_is_refused():
    assert ai._parse('{"classification": "existing_lead_comm", '
                     '"confidence": 99}') is None


def test_confidence_is_clamped_and_coerced():
    assert ai._parse('{"classification":"new_lead","confidence":500}'
                     ).confidence == 100
    assert ai._parse('{"classification":"new_lead","confidence":-20}'
                     ).confidence == 0
    assert ai._parse('{"classification":"new_lead","confidence":"eighty"}'
                     ).confidence == 0


def test_prose_around_the_json_is_tolerated():
    got = ai._parse('Sure! Here is the answer:\n'
                    '{"classification": "new_lead", "confidence": 85, '
                    '"reason": "asks for a rate"}\nHope that helps.')
    assert got.klass == K.NEW_LEAD and got.confidence == 85


def test_a_missing_reason_still_parses():
    assert ai._parse('{"classification":"internal","confidence":70}'
                     ).reason == 'no reason given'


# ─── it cannot break intake ──────────────────────────────────────────────
def test_a_failing_model_returns_nothing_rather_than_raising(monkeypatch):
    monkeypatch.setenv('LEAD_INTAKE_AI', 'on')
    monkeypatch.setenv('GROQ_API_KEY', 'x')
    monkeypatch.setattr(ai, '_ask',
                        lambda text: (_ for _ in ()).throw(
                            RuntimeError('service unavailable')))
    assert ai.opinion(msg(subject='RFQ', body=RFQ_BODY), decision()) is None


def test_the_tree_survives_a_model_that_explodes():
    def boom(m, decided):
        raise RuntimeError('down')

    ctx = li.Context(internal_domains=[PROCAM], ai_opinion=boom)
    d = li.classify(msg(subject='RFQ - transformer', body=RFQ_BODY), ctx)
    assert d.klass in K.LABELS, 'intake must not fail with the model'


def test_the_prompt_carries_the_original_sender_not_the_forwarder():
    """Most mail here is relayed by staff; judging the colleague is the
    mistake that cost 327 real enquiries earlier."""
    text = ai._as_prompt(msg(subject='Fw: RFQ', body=RFQ_BODY,
                             frm='sales@procamgroup.in',
                             _forward_resolved=True,
                             _resolved_sender='buyer@client.com'))
    assert 'From: buyer@client.com' in text
    assert 'Forwarded by: sales@procamgroup.in' in text


def test_the_body_sent_to_the_model_is_capped():
    text = ai._as_prompt(msg(subject='RFQ', body='x' * 50000))
    assert len(text) < ai._MAX_BODY + 2000


def test_the_prompt_tells_it_to_prefer_review_when_unsure():
    """A missed enquiry costs far more than a duplicate one, and the
    model should be told so rather than left to guess the trade-off."""
    assert 'needs_review' in ai._PROMPT
    assert 'missed enquiry costs' in ai._PROMPT


def test_the_step_check_is_what_protects_a_deterministic_decision():
    """Today a thread match also happens to carry no confidence, so the
    confidence guard catches it too. That is coincidence, not design: if
    a deterministic step ever records one, the step check is the only
    thing standing between a model and a fact.
    """
    thread_match = li.Decision(K.EXISTING, step=3, lead_id=11,
                               confidence=60, reason='thread match')
    assert ai.should_consult(thread_match) is False

    vendor = li.Decision(K.RATE_SOURCING, step=6, confidence=60,
                         reason='known supplier')
    assert ai.should_consult(vendor) is False

    # And the same numbers at step 10 are consulted, so the difference is
    # the step and nothing else.
    assert ai.should_consult(
        li.Decision(K.REVIEW, step=10, confidence=60, reason='x')) is True


# ─── the opinion has to survive into the review queue ────────────────────
def test_the_reviewable_payload_carries_what_the_model_said():
    """An opinion nobody can read afterwards cannot be judged.

    The whole case for shipping Phase 4 is that its accuracy will be
    visible next to the rules'. That only holds if the opinion is stored.
    """
    from app.services import lead_intake_db as db

    d = li.Decision(K.NEW_LEAD, step='10+ai', confidence=45, reason='x')
    d.extra.update({'ai_class': K.NEW_LEAD, 'ai_confidence': 88,
                    'ai_reason': 'customer asking for a rate',
                    'ai_model': 'llama-3.3-70b-versatile',
                    'ai_applied': True, 'rule_class': K.REVIEW})

    payload = db._reviewable({'subject': 'hi', 'body': {'content': 'b'}}, d)

    assert payload['ai_class'] == K.NEW_LEAD
    assert payload['ai_confidence'] == 88
    assert payload['ai_applied'] is True
    # the rule's own answer, so disagreements are countable
    assert payload['rule_class'] == K.REVIEW


def test_a_decision_with_no_opinion_adds_no_ai_keys():
    from app.services import lead_intake_db as db

    payload = db._reviewable({'subject': 'hi', 'body': {'content': 'b'}},
                             li.Decision(K.REVIEW, step=10, reason='x'))
    assert not [k for k in payload if k.startswith('ai_')]


def test_reviewable_still_works_without_a_decision():
    """The batch path calls it with one argument. It must not break."""
    from app.services import lead_intake_db as db

    payload = db._reviewable({'subject': 'hi', 'body': {'content': 'b'}})
    assert payload['body'] == 'b'
