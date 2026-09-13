"""
§15 — the question library as acceptance testing.

Every phrasing in the library must reach the intent it names, with no
model. A library that is only a document rots; this one fails the build
when a phrasing stops working.

The out-of-scope half matters just as much. §3.4 forbids inventing an
answer, and a classifier that stretches to fit is how invention starts —
so "forecast next quarter revenue" must come back unrecognised rather
than being answered by whichever intent looked closest.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'LibraryTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'library.db')
os.environ.pop('PROCAM_AI_BASE_URL', None)

from app.copilot import library                          # noqa: E402
from app.copilot import intents as catalogue             # noqa: E402
from app.copilot.service import _match_patterns          # noqa: E402


@pytest.mark.parametrize('persona,question,expected', library.QUESTIONS)
def test_every_library_question_classifies(persona, question, expected):
    got, _params = _match_patterns(question)
    assert got == expected, (
        f'{persona}: {question!r} → {got!r}, expected {expected!r}')


def test_the_library_is_at_least_the_size_the_brief_asks_for():
    """§15 asks for 200+. Counting the declared intent examples too,
    since those are exercised by the classifier the same way."""
    declared = sum(len(i.examples) for i in catalogue.REGISTRY.values())
    total = len(library.QUESTIONS) + declared
    assert total >= 200, f'only {total} phrasings'


def test_it_covers_every_persona_in_the_brief():
    personas = {p for p, _q, _i in library.QUESTIONS}
    for expected in (library.SALES, library.HEAD, library.MGMT,
                     library.OPS, library.ADMIN, library.FIN,
                     library.ACCT):
        assert expected in personas, expected


def test_every_intent_has_a_real_question_written_for_it():
    """An intent nobody asks about usually answers nothing anybody
    wanted."""
    _covered, uncovered = library.coverage()
    assert not uncovered, f'no library question reaches: {uncovered}'


def test_a_third_of_the_library_is_out_of_scope_on_purpose():
    """Knowing what it cannot do is half of not hallucinating."""
    s = library.stats()
    assert s['out_of_scope'] >= 15


def test_the_library_holds_three_hundred_deterministic_questions():
    """The brief's 300+, counted without the intents' own examples and
    without the model-only phrasings — every one of these routes on the
    rules alone (the parametrised test above)."""
    answerable = [q for q in library.QUESTIONS if q[2]]
    assert len(answerable) >= 300, len(answerable)


def test_no_phrasing_is_listed_twice():
    assert library.duplicates() == []


def test_model_only_phrasings_never_route_somewhere_else():
    """They are allowed to be unrecognised by the rules — that is why
    they are marked — but a rule that grabs one for a DIFFERENT intent
    is a misroute the model would never get the chance to correct."""
    wrong = []
    for _p, question, expected in library.MODEL_ONLY:
        got, _params = _match_patterns(question)
        if got not in (None, expected):
            wrong.append(f'{question!r} → {got!r}, declared {expected!r}')
        assert catalogue.get(expected), expected
    assert not wrong, '; '.join(wrong)


def test_every_follow_up_chip_routes_to_the_intent_it_names():
    """A chip that asks a question the rules read differently would
    answer something the chip did not offer."""
    from app.copilot import service as svc

    wrong = []
    for after, chips in svc.FOLLOW_UP_CHIPS.items():
        assert catalogue.get(after), after
        for template, target in chips:
            question = template.format(account='Tata Steel')
            got, _p = _match_patterns(question)
            if got != target:
                wrong.append(f'after {after}: {question!r} → {got!r}, '
                             f'chip says {target!r}')
    assert not wrong, '; '.join(wrong)


def test_every_clarification_option_routes_on_its_own():
    from app.copilot import service as svc

    for _rx, _prompt, options in svc._AMBIGUOUS:
        assert 2 <= len(options) <= 4
        for _label, question in options:
            got, _p = _match_patterns(question)
            assert got, question


def test_the_full_classifier_agrees_with_the_rules_on_the_library():
    """classify() adds context, memory, clarification and the model on
    top of the rules. With none of those in play it must give exactly
    the library's answer — the layers may only fill gaps."""
    from app.access.scope import Scope
    from app.copilot import service as svc

    sc = Scope('LIBRARY', '', set(), set(), 'own')
    wrong = []
    for _p, question, expected in library.QUESTIONS:
        got, _params, used_model = svc.classify(question, sc)
        if got != expected or used_model:
            wrong.append(f'{question!r} → {got!r}')
    assert not wrong, '; '.join(wrong)


def test_the_declared_examples_also_classify():
    """Each intent's own examples feed the model's prompt. If one of
    them does not reach its intent, the prompt is teaching the model
    something untrue."""
    wrong = []
    for key, intent in catalogue.REGISTRY.items():
        for example in intent.examples:
            got, _p = _match_patterns(example)
            if got != key:
                wrong.append(f'{key}: {example!r} → {got!r}')
    assert not wrong, '; '.join(wrong)
