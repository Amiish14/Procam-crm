"""
Procam AI — the orchestration, in the order §4 specifies.

    ask(question) →
        resolve scope          (never the model's business)
        classify intent        (the model's only job before the answer)
        check entitlement      (§6.6 safe form, not an error)
        run the approved query (parameterised, scope injected)
        narrate                (given the Result and nothing else)
        audit                  (extends the existing Audit Trail)

The model is optional at every step. With no model host configured the
classifier falls back to patterns and the narrator to the Result's own
headline — the Copilot answers less fluently and just as correctly.
That is not a degraded mode bolted on afterwards; it is how the thing is
built, because §12 asks for graceful fallback and because a CRM feature
that stops working when a GPU reboots is not a CRM feature.
"""
from __future__ import annotations

import json
import re
import time

from app.access import scope as scope_mod
from app.copilot import intents as catalogue
from app.copilot import model as model_mod
from app.copilot import queries as _queries          # noqa: F401 — registers
from app.copilot import vocabulary
from app.models import copilot as _copilot_model    # noqa: F401 — table


#: Conversation turns kept for follow-ups ("which is biggest?"). Small
#: on purpose: this is a cache of results the user may stop being
#: entitled to, and §16 requires a revoked permission to bite on the
#: next question.
MEMORY_TURNS = 6


class Answer:
    def __init__(self, *, intent_key, result, prose, scope_note=None,
                 clarify=None, ms=0, model_used=False, log_id=None):
        self.intent_key = intent_key
        self.result = result
        self.prose = prose
        self.scope_note = scope_note
        self.clarify = clarify
        self.ms = ms
        self.model_used = model_used
        #: The audit row, so §6.4 feedback can attach to this answer.
        self.log_id = log_id

    def to_dict(self):
        d = {'intent': self.intent_key, 'prose': self.prose,
             'clarify': self.clarify, 'ms': self.ms,
             'model_used': self.model_used, 'scope_note': self.scope_note,
             'log_id': self.log_id}
        d.update(self.result.to_dict() if self.result else
                 catalogue.Result().to_dict())
        return d


def ask(question, *, sc=None, history=None, actor=None, context=None):
    """The whole pipeline. Never raises: a failure is an answer too."""
    started = time.time()
    sc = sc or scope_mod.current()
    question = (question or '').strip()

    if not question:
        return Answer(intent_key=None, result=catalogue.Result(),
                      prose='Ask me something about the CRM.')

    key, params, model_used = classify(question, sc, history=history)
    intent = catalogue.get(key) if key else None

    if intent is None:
        # Logged like any other question. "What people asked that the
        # catalogue could not answer" is the backlog §11 asks for, and
        # returning early without auditing is how that column stays
        # permanently empty and the Copilot never improves.
        answer = _unrecognised(question, sc, started)
        answer.log_id = _audit(question, answer, sc, actor=actor)
        return answer

    if intent.permission and not sc.can(intent.permission):
        # §6.6 — never a blunt access denied. Audited too: repeated
        # refusals are a sign somebody's access is configured wrongly.
        answer = _not_entitled(intent, sc, started)
        answer.log_id = _audit(question, answer, sc, actor=actor)
        return answer

    params = dict(params or {})
    if context:
        # §6.3 — the record the panel was opened on. It only ever
        # narrows: the handler still resolves it through a scoped query,
        # so a context chip cannot reach a record the viewer may not see.
        params.setdefault('context', context)
        if context.get('type') == 'lead' and context.get('id'):
            params.setdefault('lead_id', context['id'])

    try:
        result = intent.handler(sc, params)
    except Exception:
        _log_exception(intent.key)
        result = catalogue.Result(
            headline='Something went wrong reading that from the CRM.',
            empty=True,
            notes=['The failure is logged. Nothing was changed.'])

    prose = narrate(question, intent, result) if not result.empty else \
        result.headline
    ms = int((time.time() - started) * 1000)

    answer = Answer(intent_key=intent.key, result=result, prose=prose,
                    scope_note=_scope_note(sc), ms=ms,
                    model_used=model_used)
    answer.log_id = _audit(question, answer, sc, actor=actor)
    return answer


# ── intent classification ────────────────────────────────────────────
def classify(question, sc, *, history=None):
    """(intent_key, params, used_model).

    Patterns first, because they are instant, free and testable, and
    because most questions people actually ask are near-verbatim
    repeats of the chips they clicked yesterday. The model is consulted
    only when the patterns do not recognise the question.
    """
    key, params = _match_patterns(question)
    if key:
        return key, params, False

    if model_mod.available():
        guess = model_mod.classify(question, catalogue.catalogue(),
                                   history=history)
        if guess and catalogue.get(guess.get('intent')):
            return guess['intent'], guess.get('params') or {}, True

    return None, {}, False


#: Cheap, deterministic recognition. Order matters — the first match
#: wins, so put the specific before the general.
_PATTERNS = [
    (r'\b(my day|what.*(attention|work on|pending for me)|morning brief)\b',
     'my_day', {}),
    (r'\b(stale|idle|no activity|gone quiet|untouched)\b.*\blead',
     'leads_stale', {}),
    (r'\blead.*\b(stale|idle|no activity|untouched)\b',
     'leads_stale', {}),
    (r'\b(open|active)\s+leads?\b', 'leads_open', {}),
    (r'\bleads?\b.*\bno (follow.?up|next action)\b',
     'leads_no_next_action', {}),
    # §6.3 names these three as the same question, so they resolve to
    # the same intent: "pending quote" is the canonical form the
    # vocabulary folds the others into.
    (r'\bpending quote\b', 'rfqs_unquoted', {}),
    (r'\brfq.*\b(not (yet )?quoted|unquoted|pending quot)',
     'rfqs_unquoted', {}),
    (r'\b(unquoted|not quoted)\b.*\brfq', 'rfqs_unquoted', {}),
    (r'\bquotes?\b.*\b(no (reply|response|follow.?up)|awaiting|waiting)\b',
     'quotes_awaiting_reply', {}),
    (r'\bquotes?\b.*\b(above|over|more than|greater than)\b',
     'quotes_above', {}),
    (r'\b(last|latest|most recent)\b.*\b(quote|price|rate we (gave|sent))',
     'last_quote_for_account', {'_capture': 'account'}),
    (r'\b(turn.?around|how long.*quote|rfq to quote)\b',
     'quote_turnaround', {}),
    (r'\b(weighted )?pipeline\b.*\b(worth|value|total)\b',
     'pipeline_value', {}),
    (r"\b(what'?s |show )?my pipeline\b", 'pipeline_value', {}),
    (r'\b(biggest|largest|top)\b.*\b(opportunit|deals?)\b',
     'top_opportunities', {}),
    (r'\b(stuck|stalled|not moving|sitting)\b.*\b(deal|opportunit)',
     'stalled_deals', {}),
    (r'\bwho (handles|owns|is the pic for|manages)\b', 'account_owner',
     {'_capture': 'account'}),
    (r'\b(is|are|do we|did we|does|have we)\b.*\b(already (a |an )?'
     r'(handled|customer|account|client)|work(ed)? with)\b',
     'account_status', {'_capture': 'account'}),
    (r'\b(inactive|gone quiet|not contacted|dormant)\b.*\b(account|customer)',
     'accounts_inactive', {}),
    (r'\b(account|customer)s?\b.*\b(inactive|gone quiet|dormant)\b',
     'accounts_inactive', {}),
    (r'\bhand(ed )?over', 'handovers_recent', {}),
    (r'\b(where|why).*\b(lose|losing|lost)\b', 'loss_analysis', {}),
    (r'\blost leads?\b.*\bno reason\b', 'dq_lost_no_reason', {}),
    (r'\bwho should i (call|contact|chase)\b', 'next_best_action', {}),
    (r'\b(next best action|what should i do next)\b',
     'next_best_action', {}),
    (r'^\s*(search|find|look ?up|show me)\b', 'universal_search',
     {'_capture': 'term'}),
    # ── Phase 4 · §8 ────────────────────────────────────────────────
    (r'\b(latest|last|recent)\b.*\bemail\b', 'thread_summary', {}),
    (r'\bemail (thread|trail|chain)\b', 'thread_summary', {}),
    (r'\bwhat did the (customer|client) (ask|say|want)\b',
     'thread_summary', {}),
    (r'\battachment|\bboq\b|\bcargo list\b|\benquiry sheet\b',
     'attachment_contents', {}),
    (r'\b(summar\w+|brief me on|360)\b.*\b(this )?lead\b', 'lead_360', {}),
    (r'\blead 360\b', 'lead_360', {}),
    (r'\b(summar\w+|brief me on)\b.*\b(this )?account\b',
     'account_360', {'_capture': 'account'}),
    (r'\baccount 360\b', 'account_360', {'_capture': 'account'}),
]

#: "50 lakh", "1 crore", "5000000" — the way an Indian logistics quote
#: is actually written.
_AMOUNT = re.compile(
    r'(\d[\d,]*(?:\.\d+)?)\s*(cr(?:ore)?s?|lakhs?|lacs?|l\b|k\b)?',
    re.IGNORECASE)
_SCALE = {'cr': 1e7, 'crore': 1e7, 'crores': 1e7, 'lakh': 1e5,
          'lakhs': 1e5, 'lac': 1e5, 'lacs': 1e5, 'l': 1e5, 'k': 1e3}


def parse_amount(text):
    """The first monetary figure in the text, in rupees, or None."""
    for m in _AMOUNT.finditer(text or ''):
        raw, unit = m.group(1), (m.group(2) or '').lower().rstrip('s')
        try:
            n = float(raw.replace(',', ''))
        except ValueError:
            continue
        if unit:
            return n * _SCALE.get(unit, 1)
        if n >= 1000:                 # a bare big number is rupees
            return n
    return None


_DAYS = re.compile(r'(\d+)\s*(?:\+\s*)?day', re.IGNORECASE)

#: A bare term is a search; a short question is not.
_QUESTION_WORD = re.compile(
    r'^\s*(who|what|when|where|why|how|which|is|are|do|does|did|can|'
    r'should|will|would|show|list|give)\b', re.IGNORECASE)

_SEARCH_PREFIX = re.compile(
    r'^\s*(?:search(?:\s+for)?|find|look\s?up|show\s+me)\s+',
    re.IGNORECASE)


def _search_term(question):
    s = _SEARCH_PREFIX.sub('', (question or '').strip().rstrip('?.!'), 1)
    return s.strip(' ,-')


def _match_patterns(question):
    # §7 — fold synonyms and expand abbreviations first, so one pattern
    # covers "pending quote", "quotations pending with me" and "RFQ I
    # have not quoted" without three entries in the list.
    q = ' ' + vocabulary.expand(question) + ' '
    for rx, key, spec in _PATTERNS:
        if not re.search(rx, q):
            continue
        params = {}
        days = _DAYS.search(q)
        if days:
            params['days'] = int(days.group(1))
        if key in ('quotes_above',):
            amount = parse_amount(question)
            if amount:
                params['amount'] = amount
        if spec.get('_capture') == 'account':
            name = _account_name(question)
            if name:
                params['account'] = name
        if spec.get('_capture') == 'term':
            params['term'] = _search_term(question)
        if 'weighted' in q:
            params['weighted'] = 'true'
        return key, params

    # §6.3 and the matrix's "Tata (bare search term)" row: a couple of
    # words that are plainly not a question are a search. Guarded to
    # short input so a long unrecognised sentence still gets the honest
    # "I could not tell what you are asking" rather than a search for
    # the whole sentence, which would look like an answer.
    bare = (question or '').strip().rstrip('?.!')
    if (2 <= len(bare) <= 40 and len(bare.split()) <= 4
            and not _QUESTION_WORD.match(bare)):
        return 'universal_search', {'term': bare}

    return None, {}


#: Everything before the customer's name, in the shapes people
#: actually type. Longest first, so "who is the pic for" is not left as
#: "the pic for" by the shorter "who" alternative.
_LEADING = re.compile(
    r'^\s*(?:'
    r'what(?:\s+did|\s+was|\s+is)?\s+(?:we\s+)?(?:last\s+|latest\s+|'
    r'most\s+recent\s+)?(?:quote[d]?|price[d]?|rate[d]?|offer(?:ed)?)'
    r'(?:\s+(?:to|for))?'
    r'|(?:our|the)\s+(?:last|latest|most\s+recent)\s+'
    r'(?:quote|price|rate|offer)(?:\s+(?:to|for))?'
    r'|last\s+(?:quote|price|rate)\s+(?:we\s+)?(?:gave|sent|quoted)?'
    r'(?:\s+(?:to|for))?'
    r'|(?:summari[sz]e|brief\s+me\s+on|tell\s+me\s+about)'
    r'(?:\s+the)?(?:\s+account)?'
    r'|account\s*360\s*(?:for)?'
    r'|who\s+(?:handles|owns|manages|is\s+the\s+pic\s+for)'
    r'|(?:do|did|have)\s+we\s+(?:ever\s+)?work(?:ed)?\s+with'
    r'|(?:is|are|do\s+we|does|have\s+we)'
    r')\s+', re.IGNORECASE)

#: Everything after it. "work with" is handled as a prefix above, so it
#: is deliberately not here — stripping it as a suffix ate the name.
_TRAILING = re.compile(
    r'\s*\b(?:already|handled|a\s+customer|an?\s+account|a\s+client|'
    r'in\s+the\s+crm|account|\'s\s+account)\b.*$', re.IGNORECASE)

#: What is left when the strip has eaten everything but filler.
_NOT_A_NAME = {'', 'the', 'a', 'an', 'them', 'it', 'this', 'that',
               'customer', 'account', 'client', 'company', 'us'}


def _account_name(question):
    """Pull the customer out of "who handles JSW?" and friends.

    Returns None unless a recognised prefix was actually stripped. If we
    could not find where the question stops and the name starts, we do
    not know the name — and a handler asking "which customer?" is a far
    better failure than one confidently reporting on an account called
    "what did we last quote".
    """
    raw = (question or '').strip().rstrip('?.!')
    stripped = _LEADING.sub('', raw, count=1)
    if stripped == raw:
        return None                       # no prefix found, no name known
    stripped = _TRAILING.sub('', stripped).strip(' ,-')
    if stripped.lower() in _NOT_A_NAME:
        return None
    return stripped or None


# ── narration ────────────────────────────────────────────────────────
def narrate(question, intent, result):
    """Prose about the Result, or the Result's own headline.

    The model is given the structured answer and nothing else, so there
    is nothing for it to invent. When there is no model, the headline is
    already a complete sentence — every handler writes one.
    """
    if not model_mod.available():
        return result.headline
    prose = model_mod.narrate(question, intent.label, result.to_dict())
    return prose or result.headline


# ── the paths that are not an answer ─────────────────────────────────
def _unrecognised(question, sc, started):
    suggestions = [i.label for i in catalogue.available(sc)][:6]
    return Answer(
        intent_key=None, result=catalogue.Result(empty=True),
        prose=('I could not tell what you are asking for. I can answer '
               'questions like: ' + ', '.join(suggestions) + '.'),
        clarify=suggestions, scope_note=_scope_note(sc),
        ms=int((time.time() - started) * 1000))


def _not_entitled(intent, sc, started):
    """§6.6 — say what they can do, not that they are forbidden."""
    return Answer(
        intent_key=intent.key,
        result=catalogue.Result(restricted=True, empty=True),
        prose=(f'“{intent.label}” is outside your CRM access. An '
               f'administrator can grant it on the Access Control screen.'),
        scope_note=_scope_note(sc),
        ms=int((time.time() - started) * 1000))


def _scope_note(sc):
    """Shown in the panel so an answer is never mistaken for the whole
    company when it is one person's slice."""
    from app.models.access import DataScope
    if sc.data_scope == DataScope.ALL:
        return 'Answering across the whole company.'
    if sc.data_scope == DataScope.VERTICAL:
        return f'Answering within {sc.vertical or "your vertical"}.'
    return 'Answering from your own records only.'


# ── audit — extends the existing trail, §11 ──────────────────────────
def _audit(question, answer, sc, *, actor=None):
    """One row per question. Never the model's reasoning — §11 is
    explicit — and never the answer's rows, which would duplicate
    business data into a log with different retention."""
    from app import db
    try:
        from app.models.copilot import CopilotLog

        row = CopilotLog(
            emp_code=actor or sc.emp_code or '',
            question=(question or '')[:1000],
            intent=answer.intent_key or '',
            data_scope=sc.data_scope,
            sources=', '.join((answer.result.sources
                               if answer.result else []))[:500],
            answered=bool(answer.result and not answer.result.empty),
            restricted=bool(answer.result and answer.result.restricted),
            model_used=answer.model_used,
            latency_ms=answer.ms,
        )
        db.session.add(row)
        db.session.commit()
        return row.id
    except Exception:
        # A failed audit write leaves the session in a failed
        # transaction, and the next query on it raises
        # PendingRollbackError — so a logging problem would break the
        # answer after the one it happened on. Roll back before giving
        # up, and give up quietly: the answer has already been computed
        # and the user is entitled to it.
        try:
            db.session.rollback()
        except Exception:
            pass
        _log_exception('audit')
    return None


def _log_exception(where):
    try:
        from app import app as flask_app
        flask_app.logger.exception('copilot: %s failed', where)
    except Exception:
        pass


# ── feedback, §6.4 ───────────────────────────────────────────────────
FEEDBACK_REASONS = (
    'Wrong record', 'Missing information', 'Wrong interpretation',
    'Wrong calculation', 'Access issue', 'CRM data is wrong', 'Other',
)


def record_feedback(log_id, *, helpful, reason=None, note=None, actor=None):
    from app import db
    from app.models.copilot import CopilotLog

    row = db.session.get(CopilotLog, int(log_id))
    if row is None:
        return False, 'Not found'
    if not helpful and reason and reason not in FEEDBACK_REASONS:
        return False, f'Unknown reason {reason}'
    row.helpful = bool(helpful)
    row.feedback_reason = (reason or '')[:60] or None
    row.feedback_note = (note or '')[:500] or None
    row.feedback_by = actor or ''
    db.session.commit()
    return True, None


def suggestions(sc, *, limit=10):
    """§6.2 — the chips, role-aware, so none of them can refuse."""
    return [{'key': i.key, 'label': i.label,
             'example': i.examples[0] if i.examples else i.label}
            for i in catalogue.available(sc)][:limit]
