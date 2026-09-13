"""
Procam AI — the orchestration, in the order §4 specifies.

    ask(question) →
        resolve scope          (never the model's business)
        read the context       (the record the panel is open on — only
                                if this viewer may see it)
        classify intent        (rules, then the conversation, then the
                                model — the model's only job before the
                                answer)
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

Every answer also says how sure it is and how it was reached
    `confidence` is high when a rule matched the wording directly (or the
    panel's record supplied the subject), medium when it took a synonym
    or the previous turn of the conversation, low when the model chose
    the intent. `how_answered` names the intent and the filters in
    words. Neither ever contains a query, a host or the model's
    reasoning.
"""
from __future__ import annotations

import hashlib
import hmac

import re
import time
from datetime import datetime, timedelta

from app.access import scope as scope_mod
from app.copilot import intents as catalogue
from app.copilot import model as model_mod
from app.copilot import queries as _queries          # noqa: F401 — registers
from app.copilot import insights as _insights        # noqa: F401 — registers
from app.copilot import vocabulary
from app.models import copilot as _copilot_model    # noqa: F401 — table


#: Conversation turns kept for follow-ups ("which is biggest?"). Small
#: on purpose: this is a cache of results the user may stop being
#: entitled to, and §16 requires a revoked permission to bite on the
#: next question.
MEMORY_TURNS = 6
#: A follow-up reaches back this far and no further. Past it, "what
#: about last month" is a new question, not a continuation of one asked
#: before lunch.
MEMORY_MINUTES = 30

#: How each way of reaching an intent maps to the confidence shown.
CONFIDENCE = {'pattern': 'high', 'context': 'high', 'synonym': 'medium',
              'memory': 'medium', 'model': 'low'}


class Answer:
    def __init__(self, *, intent_key, result, prose, scope_note=None,
                 clarify=None, ms=0, model_used=False, log_id=None,
                 confidence=None, how_answered=None, follow_ups=None,
                 clarification=None, conversation_id=None, context=None,
                 resolution=None):
        self.intent_key = intent_key
        self.result = result
        self.prose = prose
        self.scope_note = scope_note
        self.clarify = clarify
        self.ms = ms
        self.model_used = model_used
        #: The audit row, so §6.4 feedback can attach to this answer.
        self.log_id = log_id
        self.confidence = confidence
        self.how_answered = how_answered
        self.follow_ups = follow_ups or []
        self.clarification = clarification
        self.conversation_id = conversation_id
        self.context = context
        self.resolution = resolution

    def to_dict(self):
        d = {'intent': self.intent_key, 'prose': self.prose,
             'clarify': self.clarify, 'ms': self.ms,
             'model_used': self.model_used, 'scope_note': self.scope_note,
             'log_id': self.log_id}
        result = self.result or catalogue.Result()
        d.update(result.to_dict())
        # Added fields only — everything above keeps its old meaning.
        d['clarification'] = self.clarification or result.clarification
        d['citations'] = _citations(result)
        d['confidence'] = self.confidence
        d['how_answered'] = self.how_answered
        d['follow_ups'] = list(self.follow_ups)
        d['conversation_id'] = self.conversation_id
        d['context'] = _public_context(self.context)
        d['resolution'] = self.resolution
        return d


def _citations(result):
    """Record-level citations: the handler's own, else one per record the
    rows link to. Deduplicated, capped — a citation list longer than the
    table is noise."""
    out, seen = [], set()
    for c in list(result.citations or []) + [
            r.get('_chip') for r in (result.rows or [])
            if isinstance(r, dict) and r.get('_chip')]:
        if not isinstance(c, dict) or not c.get('type') or not c.get('id'):
            continue
        key = (c['type'], str(c['id']), c.get('source'), c.get('date'))
        if key in seen:
            continue
        seen.add(key)
        out.append({k: c[k] for k in ('type', 'id', 'label', 'source', 'date')
                    if c.get(k) is not None})
        if len(out) >= 20:
            break
    return out


# ══════════════════════════════════════════════════════════════════════
#  THE PIPELINE
# ══════════════════════════════════════════════════════════════════════
class Plan:
    """What classification decided, before anything is read."""

    def __init__(self, key=None, params=None, *, resolution=None,
                 model_used=False, notes=None, clarification=None,
                 restricted=None, context=None):
        self.key = key
        self.params = dict(params or {})
        self.resolution = resolution
        self.model_used = model_used
        self.notes = list(notes or [])
        self.clarification = clarification
        self.restricted = restricted
        self.context = context


def ask(question, *, sc=None, history=None, actor=None, context=None,
        conversation_id=None):
    """The whole pipeline. Never raises: a failure is an answer too."""
    started = time.time()
    sc = sc or scope_mod.current()
    question = (question or '').strip()

    if not question:
        return Answer(intent_key=None, result=catalogue.Result(),
                      prose='Ask me something about the CRM.')

    who = actor or sc.emp_code or ''
    start = _conversation_start(conversation_id, who)
    plan = plan_question(question, sc, history=history, context=context,
                         memory=_memory(who, start))
    answer = _answer(question, plan, sc, started)
    answer.log_id = _audit(question, answer, sc, actor=actor)
    answer.conversation_id = _conversation_after(who, start, answer.log_id)
    return answer


def _answer(question, plan, sc, started):
    """Run a Plan to an Answer. Shared by ask() and ask_stream()."""
    if plan.restricted:
        return Answer(
            intent_key=plan.key, result=catalogue.Result(
                restricted=True, empty=True, headline=plan.restricted),
            prose=plan.restricted, scope_note=_scope_note(sc),
            ms=int((time.time() - started) * 1000),
            context=plan.context, resolution='context')

    if plan.clarification:
        return Answer(
            intent_key=None, result=catalogue.Result(empty=True),
            prose=plan.clarification['question'],
            clarification=plan.clarification, scope_note=_scope_note(sc),
            ms=int((time.time() - started) * 1000), context=plan.context)

    intent = catalogue.get(plan.key) if plan.key else None
    if intent is None:
        # Logged like any other question. "What people asked that the
        # catalogue could not answer" is the backlog §11 asks for, and
        # returning early without auditing is how that column stays
        # permanently empty and the Copilot never improves.
        return _unrecognised(question, sc, started)

    if intent.permission and not sc.can(intent.permission):
        # §6.6 — never a blunt access denied. Audited too: repeated
        # refusals are a sign somebody's access is configured wrongly.
        return _not_entitled(intent, sc, started)

    params = _with_context(plan)
    result = _run(intent, sc, params)
    if plan.notes:
        result.notes = list(result.notes or []) + plan.notes
    prose = narrate(question, intent, result) if not result.empty else \
        result.headline
    return _finish(intent, result, prose, sc, plan, params, started)


def _run(intent, sc, params):
    try:
        return intent.handler(sc, params)
    except Exception:
        _log_exception(intent.key)
        return catalogue.Result(
            headline='Something went wrong reading that from the CRM.',
            empty=True,
            notes=['The failure is logged. Nothing was changed.'])


def _finish(intent, result, prose, sc, plan, params, started):
    clarifying = bool(result.clarification)
    return Answer(
        intent_key=intent.key, result=result, prose=prose,
        scope_note=_scope_note(sc),
        ms=int((time.time() - started) * 1000),
        model_used=plan.model_used,
        confidence=(None if clarifying or result.restricted
                    else CONFIDENCE.get(plan.resolution)),
        how_answered=how_answered(intent, params, result, plan, sc),
        follow_ups=([] if clarifying or result.restricted else
                    follow_ups(intent.key, params, result, sc)),
        context=plan.context, resolution=plan.resolution,
        clarification=result.clarification)


def _with_context(plan):
    """The params a handler receives, with the panel's record folded in.

    §6.3 — the record the panel was opened on only ever narrows: the
    handler still resolves it through a scoped query, so a context chip
    cannot reach a record the viewer may not see. A context the viewer
    may not see is not passed down at all.
    """
    params = dict(plan.params or {})
    ctx = plan.context
    if ctx and ctx.get('visible'):
        params.setdefault('context', {'type': ctx['type'], 'id': ctx['id']})
        if ctx['type'] == 'lead':
            params.setdefault('lead_id', ctx['id'])
    return params


# ── §6.3 streaming ───────────────────────────────────────────────────
def ask_stream(question, *, sc=None, history=None, actor=None,
               context=None, conversation_id=None):
    """Yield the answer in stages, as it becomes known.

    Deliberately staged rather than token-by-token. The stages are the
    real milestones — we know the intent before we have the data, and
    the data before the prose — so the panel can show what it is doing
    instead of an undifferentiated spinner. Token streaming only has
    something to add once a model is writing the prose, and it slots
    into the 'prose' stage when one is.

    Every stage is a complete, valid answer fragment. A client that
    stops reading early has fewer stages, never a broken one.
    """
    started = time.time()
    sc = sc or scope_mod.current()
    question = (question or '').strip()
    if not question:
        yield {'stage': 'done', 'prose': 'Ask me something about the CRM.'}
        return

    yield {'stage': 'thinking', 'scope_note': _scope_note(sc)}

    who = actor or sc.emp_code or ''
    start = _conversation_start(conversation_id, who)
    plan = plan_question(question, sc, history=history, context=context,
                         memory=_memory(who, start))
    intent = catalogue.get(plan.key) if plan.key else None

    def done(answer):
        answer.log_id = _audit(question, answer, sc, actor=actor)
        answer.conversation_id = _conversation_after(who, start,
                                                     answer.log_id)
        return {'stage': 'done', **answer.to_dict()}

    if (intent is None or plan.restricted or plan.clarification
            or (intent.permission and not sc.can(intent.permission))):
        if intent is not None and not plan.restricted \
                and not plan.clarification:
            yield {'stage': 'intent', 'intent': intent.key,
                   'label': intent.label}
        yield done(_answer(question, plan, sc, started))
        return

    yield {'stage': 'intent', 'intent': intent.key, 'label': intent.label}

    params = _with_context(plan)
    result = _run(intent, sc, params)
    if plan.notes:
        result.notes = list(result.notes or []) + plan.notes

    # The table is ready before the prose is. Send it, so the numbers
    # are on screen while the sentence is still being written.
    yield {'stage': 'result', **result.to_dict(),
           'citations': _citations(result)}

    prose = (narrate(question, intent, result) if not result.empty
             else result.headline)
    yield done(_finish(intent, result, prose, sc, plan, params, started))


# ══════════════════════════════════════════════════════════════════════
#  CLASSIFICATION
# ══════════════════════════════════════════════════════════════════════
#: §6.3 — a follow-up carries no subject of its own. "who owns it?"
#: after an account question means that account; "which is biggest?"
#: after a pipeline question means those opportunities. Resolved
#: without a model, because a two-word follow-up is the commonest
#: thing anybody types and it should not need a GPU.
_FOLLOW_UPS = [
    (r'^\s*(which|what)\s+is\s+(the\s+)?(biggest|largest)\b',
     'top_opportunities'),
    (r'^\s*who\s+owns?\s+(it|that|them)\b', 'account_owner'),
    (r'^\s*who\s+(handles|manages)\s+(it|that|them)\b', 'account_owner'),
    (r'^\s*(when|what)\s+(was|is)\s+the\s+last\s+(contact|activity)\b',
     'lead_360'),
    (r'^\s*(and\s+)?(the\s+)?(email|emails)\b', 'thread_summary'),
    (r'^\s*(summari[sz]e|brief me)\s*(it|that)?\s*$', 'lead_360'),
    (r'^\s*why\b', 'loss_analysis'),
]


def _missing_subject(key, params):
    """True when an intent needs a name and the question gave none."""
    intent = catalogue.get(key)
    if intent is None:
        return False
    if 'account' in intent.params and not (params.get('account')
                                           or params.get('account_id')):
        return True
    return False


def _carry_over(question, history):
    """(intent, params) inherited from the previous turn, or (None, {}).

    Only the account name is carried. Carrying more would let a stale
    filter silently shape a new answer — and the previous turn's rows
    may already be outside what this user is entitled to, since the
    scope is re-resolved on every question.
    """
    if not history:
        return None, {}
    q = ' ' + (question or '').lower().strip() + ' '
    for rx, key in _FOLLOW_UPS:
        if re.search(rx, q):
            params = {}
            for earlier in reversed(list(history)[-4:]):
                name = _account_name(str(earlier))
                if name:
                    params['account'] = name
                    break
            return key, params
    return None, {}


def classify(question, sc, *, history=None):
    """(intent_key, params, used_model).

    Patterns first, because they are instant, free and testable, and
    because most questions people actually ask are near-verbatim
    repeats of the chips they clicked yesterday. The model is consulted
    only when the patterns do not recognise the question.
    """
    plan = plan_question(question, sc, history=history)
    return plan.key, plan.params, plan.model_used


def plan_question(question, sc, *, history=None, context=None, memory=None):
    """A Plan for one question, deterministic before it is probabilistic.

        1. a question about "this" record, when the panel is open on one
        2. the rules, on the words as typed and with synonyms folded in
        3. the record in context, for a rule that matched without a subject
        4. a follow-up to the previous turn of this user's conversation
        5. the older client-side carry-over ("who owns it?")
        6. a question too broad to guess at → a clarification
        7. a bare name → search
        8. the model, if one is configured
    """
    ctx = resolve_context(sc, context)
    q_low = ' ' + (question or '').lower().strip() + ' '

    if ctx and _DEICTIC.search(q_low):
        plan = _context_plan(question, sc, ctx)
        if plan is not None:
            return plan

    key, params, how = _match_rules(question)
    if key and not _missing_subject(key, params):
        return Plan(key, params, resolution=how, context=ctx)

    if ctx:
        if key and _missing_subject(key, params) and ctx['type'] == 'company':
            if not ctx['visible']:
                return _restricted_context(ctx, key)
            return Plan(key, dict(params, account_id=ctx['id']),
                        resolution='context', context=ctx)
        if key is None and len(q_low.split()) <= 4:
            plan = _context_plan(question, sc, ctx)
            if plan is not None:
                return plan

    if memory and not key:
        state = _memory_state(memory)
        if state:
            plan = _follow_up(question, state, sc)
            if plan is not None:
                plan.context = ctx
                return plan

    # A follow-up gets its say when the patterns produced nothing, or
    # produced an intent with no subject — "who owns it?" matches the
    # account rule and captures "it", which is not a company. A complete
    # question is never reinterpreted this way, because it would have
    # kept its subject.
    carried_history = history or [m['question'] for m in (memory or [])]
    carried, carried_params = _carry_over(question, carried_history)
    if carried:
        return Plan(carried, carried_params, resolution='memory',
                    context=ctx)
    if key:
        return Plan(key, params, resolution=how, context=ctx)

    clarification = _ambiguous(question, sc)
    if clarification:
        return Plan(clarification=clarification, context=ctx)

    bare = _bare_term(question)
    if bare:
        return Plan('universal_search', {'term': bare}, resolution='pattern',
                    context=ctx)

    if model_mod.available():
        guess = model_mod.classify(question, catalogue.catalogue(),
                                   history=carried_history)
        if guess and catalogue.get(guess.get('intent')):
            return Plan(guess['intent'], guess.get('params') or {},
                        resolution='model', model_used=True, context=ctx)

    return Plan(context=ctx)


#: Cheap, deterministic recognition. Order matters — the first match
#: wins, so put the specific before the general.
#:
#: A spec may carry fixed params ({'overdue': 'true'}), `_capture` (the
#: older name extractors), `_group` (take the account from the regex's
#: `account…` named group, matched on the words as typed — never on the
#: synonym-expanded text, whose appended words would end up in the
#: name) and `_require` (skip this rule when that param came out empty,
#: so "pipeline for this month" falls through to the next rule instead
#: of asking which account "this month" is).
_PATTERNS = [
    # ── health first: "how healthy is my pipeline" is not "my pipeline" ─
    (r'\bpipeline (health|hygiene|quality|check)\b|'
     r'\bhealth (of|check on|check of) (the |my |our )?pipeline\b|'
     r'\bhow healthy is (the |my |our )?pipeline\b|'
     r'\bis (the |my |our )?pipeline healthy\b', 'pipeline_health', {}),

    (r'\bfollow.?ups?\b.*\b(due|pending|overdue|this week|today)\b',
     'followups_due', {}),
    (r'\b(pending|overdue|due)\b.*\bfollow.?ups?\b', 'followups_due', {}),
    (r'\b(my day|what.*(attention|work on|pending for me)|morning brief)\b',
     'my_day', {}),
    (r'\b(my|open|overdue|pending) tasks\b|'
     r'\btasks? (assigned to me|in my queue|for me)\b|'
     r'\bmy (work queue|task list|to.?do list)\b', 'my_tasks', {}),

    # ── leads ───────────────────────────────────────────────────────
    (r'\bwho (has|is carrying|holds) (the )?(most|fewest|least|too many) '
     r'(open )?(leads|deals|opportunities|work)\b', 'team_workload', {}),
    (r'\b(unassigned|unowned|ownerless|orphan(ed)?)\s+leads?\b',
     'leads_unassigned', {}),
    (r"\bleads?\b.*\b(not assigned|no owner|without (an? )?owner|"
     r"nobody('s| is)? (owns?|working))\b", 'leads_unassigned', {}),
    (r'\bleads?\b.*\bno (follow.?up|next action)\b',
     'leads_no_next_action', {}),
    (r'\bno (follow.?up|next action)\b.*\bleads?\b',
     'leads_no_next_action', {}),
    (r'\bleads?\b.*\bwithout (a |any )?(follow.?up|next action|next step)',
     'leads_no_next_action', {}),
    (r'\b(stale|idle|no activity|untouched)\b.*\blead',
     'leads_stale', {}),
    (r'\blead.*\b(stale|idle|no activity|untouched)\b',
     'leads_stale', {}),
    (r'\b(new|fresh)\s+(?:[a-z]+\s+){0,2}leads?\b|'
     r'\bleads?\b.*\b(came in|come in|created|added|arrived)\b|'
     r'\bhow many leads\b.*\b(today|yesterday|this|last|past)\b',
     'leads_new', {}),
    (r'\bleads?\b.*\bby stage\b|\blead (funnel|pipeline)\b|'
     r'\bstage.?wise leads?\b|\bleads? (per|at|in) (each )?stage\b|'
     r'\bleads are (at|in) each stage\b', 'leads_by_stage', {}),
    (r'\blead sources?\b|\bleads?\b.*\bby (source|channel)\b|'
     r'\bwhere (do|did|are) (our |my |the )?leads? (come|coming) from\b|'
     r'\bsource.?wise leads?\b', 'lead_sources', {}),
    (r'\b(open|active)\s+(?:[a-z]+\s+){0,2}leads?\b', 'leads_open', {}),
    (r'\bleads?\b.*\b(are|that are)?\s*open\b', 'leads_open', {}),

    # ── RFQs and quotes ─────────────────────────────────────────────
    (r'\b(quotes?|quotations?)\b.*\b(approv\w*|sign.?off)\b|'
     r'\b(pending|awaiting|waiting for|needs?|need my)\s+(my\s+)?approval\b',
     'quotes_pending_approval', {}),
    (r'\b(quotes?|quotations?|offers?)\b.*\b(expir\w*|validity|lapse|'
     r'lapsing|valid (till|until))\b|'
     r'\b(expir\w*|validity)\b.*\b(quotes?|quotations?|offers?)\b',
     'quotes_expiring', {}),
    (r'\b(quotes?|quotations?)\b.*\bby status\b|'
     r'\bquot(e|ation) (status|summary)\b|'
     r'\bstatus of (all |our |my |the )?quot(es|ations)\b',
     'quotes_by_status', {}),
    (r'\brfqs?\b.*\bby status\b|\brfq (status|summary)\b|'
     r'\bstatus of (all |our |my |the )?rfqs\b', 'rfqs_by_status', {}),
    (r'\b(recent|latest|new)\s+(quotes|quotations)\b|'
     r'\b(quotes|quotations)\b.*\b(sent|submitted|issued|raised|made|'
     r'prepared)\b', 'quotes_recent', {}),
    (r'\brfqs?\b.*\b(overdue|past (their |the )?quote.?by|late)\b|'
     r'\boverdue rfqs?\b', 'rfqs_unquoted', {'overdue': 'true'}),
    (r'\brfqs?\b.*\b(received|came in|come in|arrived)\b|'
     r'\b(recent|latest)\s+rfqs\b|'
     r'\bnew rfqs?\b(?!.*\b(above|over|more than|greater than|lakhs?|lacs?|'
     r'crores?|cr)\b)', 'rfqs_recent', {}),
    (r'\b(turn.?around|rfq to quote|how long.*\bquote)\b',
     'quote_turnaround', {}),
    (r'\bpending quote\b', 'rfqs_unquoted', {}),
    (r'\brfq.*\b(not (yet )?quoted|unquoted|pending quot|to quote)',
     'rfqs_unquoted', {}),
    (r'\b(unquoted|not quoted)\b.*\brfq', 'rfqs_unquoted', {}),
    (r'\bquotes?\b.*\b(no (reply|response|follow.?up)|awaiting|waiting|'
     r'not answered|no answer)\b', 'quotes_awaiting_reply', {}),
    (r'\bquotes?\b.*\b(above|over|more than|greater than)\b',
     'quotes_above', {}),
    (r'\b(big|large|major)\s+quotes?\b', 'quotes_above', {}),
    (r'\b(last|latest|most recent)\b.*\b(quote|price|rate we (gave|sent))',
     'last_quote_for_account', {'_capture': 'account'}),

    # ── pipeline ────────────────────────────────────────────────────
    (r'\b(deals?|opportunit\w*)\b.*\b(at.?risk|risky|in danger|slipping|'
     r'likely to slip)\b|\b(at.?risk|risky)\s+(deals?|opportunit\w*)\b|'
     r'\bopportunity risk\b|\brisk (score|of|on)\b.*\b(deal|opportunit)',
     'opportunity_risk', {}),
    (r'\b(deals?|opportunit\w*)\b.*\b(past|overdue|missed|passed)\b.*'
     r'\bclose|\bclose dates?\b.*\b(passed|past|overdue|missed|gone)\b|'
     r'\b(overdue|slipped) (deals?|opportunit\w*)\b',
     'opps_overdue_close', {}),
    (r'\b(?:pipeline|open deals?|opportunit(?:y|ies)|deals)\s+'
     r'(?:for|with|at|on)\s+(?P<account>.+)$', 'account_pipeline',
     {'_group': True, '_require': 'account'}),
    (r'\bweighted pipeline\b', 'pipeline_value', {'weighted': 'true'}),
    (r'\b(pipeline|funnel)\b.*\b(worth|value|total)\b',
     'pipeline_value', {}),
    (r'\b(worth|value|total)\b.*\b(pipeline|funnel)\b',
     'pipeline_value', {}),
    (r"\b(what'?s |show )?my pipeline\b", 'pipeline_value', {}),
    (r'^\s*pipeline\s*$', 'pipeline_value', {}),
    (r'\bpipeline\s+(for|in|of|on)\b(?!.*\bby (stage|vertical|owner|city|'
     r'customer)\b)', 'pipeline_value', {}),
    (r'\b(stuck|stalled|not moving|sitting still)\b.*\b(deal|opportunit)',
     'stalled_deals', {}),
    (r'\b(deal|opportunit)\w*\b.*\b(stuck|stalled|not moving|'
     r'sitting still|stale)\b', 'stalled_deals', {}),
    (r'\b(stale|idle|dormant)\s+(deals?|opportunit)', 'stalled_deals', {}),
    (r'\b(biggest|largest|top)\b.*\b(opportunit|deal)',
     'top_opportunities', {}),

    # ── accounts ────────────────────────────────────────────────────
    (r'\b(accounts?|customers?|clients?|compan(y|ies))\b.*'
     r'\b(no|without|missing)\s+(an?\s+)?(owner|pic|account manager)\b|'
     r'\b(unowned|ownerless|unassigned)\s+(accounts?|customers?|clients?|'
     r'compan(y|ies))\b', 'dq_accounts_no_owner', {}),
    (r'\bwho (?:is|are) (?:the |our |my )?(?:main |key |primary )?'
     r'(?:contacts?|decision.?makers?|point of contact|poc|spoc)\s+'
     r'(?:at|for|in|with|on)\s+(?P<account>.+)$|'
     r'\b(?:key|main|primary) contacts?\s+(?:at|for|in|with|on)\s+'
     r'(?P<account2>.+)$|'
     r'\bdecision.?makers?\s+(?:at|for|in)\s+(?P<account3>.+)$|'
     r'\bcontacts?\s+(?:at|for)\s+(?P<account4>.+)$|'
     r'\bwho should i (?:speak|talk) to at\s+(?P<account5>.+)$|'
     r'\bwho should i (?:contact|call) at\s+(?P<account6>.+)$',
     'key_contacts', {'_group': True}),
    (r"\b(?:key|main|primary) contacts?\b|\bdecision.?makers?\b|"
     r"\bwho (?:is|are) (?:the |our )?(?:customer'?s? )?"
     r"(?:contact|contacts|point of contact|poc|spoc)\b", 'key_contacts',
     {}),
    (r'\bwho (?:at procam |here |in (?:the |our )?team |else )?'
     r'(?:knows|has (?:worked|dealt) with|is close to|'
     r'has a relationship with|talks to|is in touch with)\s+'
     r'(?P<account>.+)$|'
     r'\brelationship (?:map|intelligence) (?:for|of|with|on)\s+'
     r'(?P<account2>.+)$', 'relationship_map', {'_group': True}),
    (r'\brelationship (map|intelligence)\b', 'relationship_map', {}),
    (r'\bcross.?sell(?:ing)?\s+(?:for|to|at|with|into|on)\s+'
     r'(?P<account>.+)$|'
     r'\bwhat (?:else )?can we (?:sell|offer)(?: to)?\s+(?P<account2>.+)$|'
     r'\b(?:which|what) services does\s+(?P<account3>.+?)\s+'
     r'(?:not |never )?(?:buy|use|take)\b|'
     r'\bwhat does\s+(?P<account4>.+?)\s+(?:buy|use) from us\b|'
     r'\bup.?sell\s+(?:to|for)\s+(?P<account5>.+)$', 'cross_sell_account',
     {'_group': True, '_require': 'account'}),
    (r'\b(?:account|customer|client) health\s+(?:for|of|on)\s+'
     r'(?P<account>.+)$|'
     r'\bhealth (?:score |check )?(?:of|for|on)\s+(?P<account2>.+)$|'
     r'\bhow healthy is\s+(?P<account3>.+)$', 'account_health',
     {'_group': True, '_require': 'account'}),
    (r'\bcustomer health\b|\b(customers?|clients?)\b.*\b(at risk|unhealthy|'
     r'in trouble)\b|\bat.?risk (customers|clients)\b', 'account_health',
     {'segment': 'customers'}),
    (r'\baccount health\b|\baccounts?\b.*\b(at risk|unhealthy|in trouble)\b|'
     r'\bat.?risk accounts\b|\bhealth of (my|our) accounts\b',
     'account_health', {}),
    (r'\b(top|biggest|largest|best|key|major)\s+(\d+\s+)?(accounts|customers|'
     r'clients)\b|\b(accounts|customers|clients)\b.*\bby (revenue|value|'
     r'business|won|billing)\b|\bwho are (our|my) (biggest|top|best|largest) '
     r'(customers|accounts|clients)\b', 'top_accounts', {}),
    (r'\bwho (handles|owns|is the pic for|manages|is looking after)\b',
     'account_owner', {'_capture': 'account'}),
    (r'\bwhose account\b', 'account_owner', {'_capture': 'account'}),
    (r'\b(is|are|do we|did we|does|have we)\b.*\b(already (a |an )?'
     r'(handled|customer|account|client)|work(ed)? with)\b',
     'account_status', {'_capture': 'account'}),
    (r'\b(new|existing) customer\b', 'account_status',
     {'_capture': 'account'}),
    (r'\b(inactive|dormant|not contacted|gone quiet)\b.*'
     r'\b(account|customer|client)', 'accounts_inactive', {}),
    (r'\b(account|customer|client)s?\b.*\b(inactive|dormant|not contacted|'
     r'gone quiet|have not contacted)\b', 'accounts_inactive', {}),

    # ── handovers ───────────────────────────────────────────────────
    (r'\bhandovers?\b.*\b(missing|no)\b.*\b(po|purchase order)\b',
     'handover_missing_po', {}),
    (r'\bhandovers?\b.*\bmissing\b', 'handover_missing_po', {}),
    (r'\b(awaiting|waiting for|pending|yet to (get|receive))\s+(a\s+|the\s+)?'
     r'(customer\s+)?(po|purchase orders?)\b|\bpending handovers?\b|'
     r'\bhandovers?\b.*\b(pending|waiting|awaiting|queue|backlog|stuck)\b|'
     r'\b(won deals?|wins)\b.*\b(awaiting|waiting|pending)\b',
     'handovers_awaiting_po', {}),
    (r'\b(won deals?|wins?)\b.*\bno (po|purchase order|handover)\b',
     'handover_missing_po', {}),
    (r'\bhand(ed |ing )?over', 'handovers_recent', {}),

    # ── loss and data quality ───────────────────────────────────────
    (r'\b(loss|lost)\s+reasons?\b', 'loss_analysis', {}),
    (r'\b(where|why)\b.*\b(lose|losing|lost)\b', 'loss_analysis', {}),
    (r'\bloss(es)?\b.*\bno reason\b', 'dq_lost_no_reason', {}),
    (r'\blost leads?\b.*\bno reason\b', 'dq_lost_no_reason', {}),
    (r'\blost (leads?|deals?|opportunit\w*)\b.*\bwithout (a |any )?reason',
     'dq_lost_no_reason', {}),
    (r'\bwhat (have|did) (we|i) lose\b|'
     r'\b(recent|latest) (losses|lost deals|lost leads)\b|'
     r'\b(deals?|leads?|opportunit\w*|business) (we )?lost\b'
     r'(?!.*\bno reason)|'
     r'\blost (deals?|leads?|opportunit\w*|business)\b(?!.*\bno reason)'
     r'(?=.*\b(this|last|past|today|yesterday|recent|recently)\b)',
     'deals_lost_recent', {}),
    (r'\bwhat (have|did) (we|i) (win|won)\b|\b(recent|latest|new) '
     r'(wins|won deals)\b|'
     r'\b(deals?|opportunit\w*|orders?|business) (we )?won\b'
     r'(?!.*\bno (po|purchase order|handover)\b)|'
     r'\bwon (deals?|opportunit\w*|business|orders?)\b'
     r'(?!.*\b(no|awaiting|waiting|pending) (po|purchase order|handover)\b)'
     r'(?=.*\b(this|last|past|today|yesterday|recent|recently|days?)\b)',
     'deals_won_recent', {}),
    (r'\b(intake|triage|classification) (review )?(queue|backlog|review)\b|'
     r'\bemails? (waiting|pending|awaiting) (for )?(review|triage|'
     r'classification)\b|\bunreviewed (emails?|enquir(y|ies)|mails?)\b',
     'intake_review_pending', {}),

    # ── next best action ────────────────────────────────────────────
    (r'\bwho should i (call|contact|chase)\b', 'next_best_action', {}),
    (r'\b(next best action|what should i do next)\b',
     'next_best_action', {}),

    # ── Phase 4 · §8 ────────────────────────────────────────────────
    (r'\b(latest|last|recent)\b.*\bemail\b', 'thread_summary', {}),
    (r'\bemail (thread|trail|chain)\b', 'thread_summary', {}),
    (r'\bwhat did the (customer|client) (ask|say|want)\b',
     'thread_summary', {}),
    (r'\battachment|\bboq\b|\bcargo list\b|\benquiry sheet\b',
     'attachment_contents', {}),
    (r'\b(summar\w+|brief me on|360)\b.*\b(this )?lead\b',
     'lead_360', {}),
    (r'\blead 360\b', 'lead_360', {}),
    (r"\bstory with (this )?lead\b", 'lead_360', {}),
    (r'\b(summar\w+|brief me on)\b.*\baccount\b',
     'account_360', {'_capture': 'account'}),
    (r'\baccount 360\b', 'account_360', {'_capture': 'account'}),
    # "summarise Tata Steel" with no other noun is an account. Placed
    # after the lead rules above, so "summarise this lead" still wins.
    (r'^\s*(summari[sz]e|brief me on|tell me about)\b', 'account_360',
     {'_capture': 'account'}),

    # ── the matrix rows built later ─────────────────────────────────
    (r'\bpipeline\b.*\bby (stage|vertical|owner|city|customer)\b',
     'pipeline_by_stage', {'_capture': 'by'}),
    (r'\bbreak ?down\b.*\b(pipeline|funnel)\b', 'pipeline_by_stage', {}),
    (r"\bwho (has ?n'?t|has not|hasnt)\b.*\b(updated|logged|touched)\b",
     'team_activity_gap', {}),
    (r'\bteam activity\b', 'team_activity_gap', {}),
    (r"\b(team|my team'?s?|everyone'?s?|the team'?s?) (workload|load|"
     r"capacity)\b|\bworkload\b|"
     r"\bwho (has|is carrying|holds) (the )?(most|fewest|least|too many) "
     r"(open )?(leads|deals|opportunities|work)\b|"
     r"\b(leads|deals|opportunities) per (person|salesperson|rep|head|"
     r"owner|employee)\b", 'team_workload', {}),
    (r'\b(win|conversion|hit) rates?\b.*\bby (vertical|service|'
     r'business line|segment|division)\b|'
     r'\b(vertical|service).?wise (win|conversion|hit) rates?\b',
     'win_rate_by_vertical', {}),
    (r'\bmy (own |personal )?(win|hit|strike) rate\b|'
     r'\bmy (own|personal) conversion( rate)?\b|'
     r'\bhow many (deals|opportunities) have i (won|converted|closed)\b',
     'my_win_rate', {}),
    (r'\b(conversion|win) rate\b', 'conversion_rate', {}),
    (r'\bquote to order\b', 'conversion_rate', {}),
    (r'\bwhat happened\b.*\b(today|sales)\b', 'daily_digest', {}),
    (r'\b(sales today|today.s activity|what changed today)\b',
     'daily_digest', {}),
    (r'\b(new|big|large|high.?value)\b.*\brfqs?\b',
     'rfqs_high_value_recent', {}),
    (r'\brfqs?\b.*\b(above|over|this week)\b',
     'rfqs_high_value_recent', {}),
    (r'\bcross.?sell\b', 'cross_sell_gap', {}),
    (r'\bsingle service\b', 'cross_sell_gap', {}),
    (r'\baccounts?\b.*\buse[sd]?\b.*\bbut (never|not)\b', 'cross_sell_gap',
     {}),
    (r'\b(won deals?|wins?)\b.*\bno (po|purchase order|handover)\b',
     'handover_missing_po', {}),
    (r'\b(data quality|incomplete records|missing (an? )?(owner|field|'
     r'value|vertical))\b', 'dq_missing_fields', {}),
    (r'\bleads?\b.*\bmissing\b', 'dq_missing_fields', {}),
    (r'\bleads?\b.*\bno (value|vertical)\b', 'dq_missing_fields', {}),
    (r'\bduplicate (account|customer|compan)', 'dq_duplicates', {}),
    (r'\b(account|customer|compan)\w*\b.*\bsame name\b',
     'dq_duplicates', {}),
    (r'\b(how am i doing|my numbers|my performance|what have i booked)\b',
     'my_performance', {}),
    (r'\b(likely to close|closing this month|likely bookings)\b',
     'closing_this_month', {}),

    # ── §4 text retrieval ───────────────────────────────────────────
    (r'\b(what did (anyone|someone|we|they) say|mentions? of|'
     r'anything about)\b', 'search_text', {'_capture': 'text'}),
    (r'\bsearch the (emails?|notes?|text)\b', 'search_text',
     {'_capture': 'text'}),

    # ── search, LAST ────────────────────────────────────────────────
    # A "show me" or "find" prefix is the weakest signal in the list, so
    # it may only claim a question no specific intent recognised.
    (r'^\s*(search|find|look ?up|show me)\b', 'universal_search',
     {'_capture': 'term'}),
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

#: "this week", "last month", "past 2 weeks" — a window in days, for
#: intents that take one. A number of days typed outright wins.
_WINDOW = re.compile(
    r'\b(?:(?:in\s+)?(?:the\s+)?(?:last|past)\s+(\d+)\s*(weeks?|months?)|'
    r'(this|last|past|previous)\s+(week|month|quarter|year)|'
    r'(today|yesterday))\b', re.IGNORECASE)
_WINDOW_DAYS = {'week': 7, 'month': 30, 'quarter': 90, 'year': 365}


def window_days(text):
    """The window a phrase names, in days, or None."""
    m = _WINDOW.search(text or '')
    if not m:
        return None
    if m.group(1):
        unit = m.group(2).lower()
        return int(m.group(1)) * (7 if unit.startswith('week') else 30)
    if m.group(4):
        return _WINDOW_DAYS[m.group(4).lower()]
    return {'today': 1, 'yesterday': 2}[m.group(5).lower()]


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


_TEXT_PREFIX = re.compile(
    r'^.*?\b(?:say(?:\s+about)?|mentions?\s+of|anything\s+about|'
    r'search\s+the\s+(?:emails?|notes?|text)\s*(?:for)?)\s+',
    re.IGNORECASE)


def _text_term(question):
    """What to look for, with the asking-words removed."""
    s = _TEXT_PREFIX.sub('', (question or '').strip().rstrip('?.!'), 1)
    return s.strip(' ,-') or (question or '').strip()


def _match_patterns(question):
    """(intent, params) from the rules alone — the library's contract."""
    key, params, _how = _match_rules(question)
    if key:
        return key, params
    bare = _bare_term(question)
    if bare:
        return 'universal_search', {'term': bare}
    return None, {}


def _match_rules(question):
    """(intent, params, 'pattern'|'synonym') or (None, {}, None).

    §7 — synonyms are folded in and abbreviations expanded first, so one
    pattern covers "pending quote", "quotations pending with me" and
    "RFQ I have not quoted" without three entries in the list. Whether
    the rule needed that folding is what separates a direct match from
    a synonym match, and so high confidence from medium.
    """
    raw_q = ' ' + (question or '').lower().strip() + ' '
    q = ' ' + vocabulary.expand(question) + ' '
    for rx, key, spec in _PATTERNS:
        grouped = spec.get('_group')
        m = re.search(rx, raw_q if grouped else q)
        if not m:
            continue
        params = {k: v for k, v in spec.items() if not k.startswith('_')}
        days = _DAYS.search(q)
        if days:
            params['days'] = int(days.group(1))
        if spec.get('_capture') == 'account':
            name = _account_name(question)
            if name:
                params['account'] = name
        if spec.get('_capture') == 'term':
            params['term'] = _search_term(question)
        if spec.get('_capture') == 'text':
            params['term'] = _text_term(question)
        if spec.get('_capture') == 'by':
            dim = re.search(r'\bby (stage|vertical|owner|city|customer)\b',
                            q)
            if dim:
                params['by'] = dim.group(1)
        if grouped:
            name = _group_name(m, question)
            if name:
                params['account'] = name
        if spec.get('_require') and not params.get(spec['_require']):
            continue
        if 'weighted' in q:
            params['weighted'] = 'true'
        declared = (catalogue.get(key).params
                    if catalogue.get(key) else {})
        if key in ('quotes_above', 'rfqs_high_value_recent'):
            amount = parse_amount(question)
            if amount:
                params['amount'] = amount
        if 'days' in declared and 'days' not in params:
            window = window_days(question)
            if window:
                params['days'] = window
        if 'vertical' in declared:
            service = vocabulary.service_in_text(question)
            if service:
                params['vertical'] = service
        how = 'pattern' if (grouped or re.search(rx, raw_q)) else 'synonym'
        return key, params, how
    return None, {}, None


def _bare_term(question):
    """§6.3 and the matrix's "Tata (bare search term)" row.

    The guard that matters is the capital letter. A bare search term
    in a CRM is a name — Tata, JSW Steel, Godrej — and requiring one
    is what stops "outstanding receivables" and "live vehicle
    tracking" being answered as searches when the honest reply is
    that the catalogue does not cover them. Getting that wrong would
    dress a gap up as an answer, which §3.4 forbids.
    """
    bare = (question or '').strip().rstrip('?.!')
    words = bare.split()
    if (2 <= len(bare) <= 40 and len(words) <= 4
            and not _QUESTION_WORD.match(bare)
            and any(w[:1].isupper() for w in words)):
        return bare
    return None


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


#: Words that end a captured name without being part of it.
_NAME_TAIL = re.compile(
    r'\s+(?:(?:this|last|next|past|previous)\s+(?:week|month|quarter|year)|'
    r'today|yesterday|(?:in|over|for)?\s*(?:the\s+)?(?:last|past)\s+\d+\s+'
    r'(?:days?|weeks?|months?)|right now|now|please)\s*$', re.IGNORECASE)
_NOT_A_NAME_START = re.compile(
    r'^(?:my|our|your|this|that|these|those|me|us|it|them|here|there|each|'
    r'every|all|any|which|what|whom?|him|her|no|not|without|missing|'
    r'open|overdue)\b', re.IGNORECASE)


def _clean_name(raw):
    """A captured account name, or None when what was captured is filler,
    a pronoun, a time window or a service rather than a customer."""
    s = (raw or '').strip().strip('?.!,;:"\'')
    s = _NAME_TAIL.sub('', s)
    s = re.sub(r'^(?:the|a|an)\s+', '', s, flags=re.IGNORECASE)
    s = re.sub(r"(?:'s)?\s+(?:account|customer|client)$", '', s,
               flags=re.IGNORECASE)
    s = s.strip(' ,-?.!')
    low = s.lower()
    if not s or low in _NOT_A_NAME or len(s) > 80:
        return None
    if _NOT_A_NAME_START.match(low):
        return None
    if re.fullmatch(r'(?:this|last|next|past)\s+\w+|\d+\s*\w*', low):
        return None
    if low in _SERVICE_PHRASES:
        return None
    return s


_SERVICE_PHRASES = ({p for p, _s in vocabulary._SERVICE_WORDS}
                    | {a.lower() for aliases in
                       vocabulary.SERVICE_ALIASES.values() for a in aliases})


def _group_name(match, question):
    """The account from a rule's named group, in the casing typed.

    The rule ran on ' ' + question.lower().strip() + ' ', so a span in
    the match is one character right of the same span in the stripped
    question — unless lower-casing changed the length (a handful of
    non-ASCII letters do), in which case the lower-case text is used.
    """
    stripped = (question or '').strip()
    for name, value in match.groupdict().items():
        if not name.startswith('account') or value is None:
            continue
        start, end = match.span(name)
        if len(stripped.lower()) == len(stripped):
            value = stripped[max(start - 1, 0):max(end - 1, 0)]
        return _clean_name(value)
    return None


# ── §6.3 the record the panel is open on ─────────────────────────────
#: A question that points at the page rather than naming a subject.
_DEICTIC = re.compile(
    r"\b(this|here|it|its|current|this one|this record|this lead|"
    r"this account|this deal|this opportunity|this customer)\b",
    re.IGNORECASE)

_KIND = {'lead': 'lead', 'leads': 'lead', 'opp': 'opportunity',
         'opportunity': 'opportunity', 'opportunities': 'opportunity',
         'company': 'company', 'companies': 'company', 'account': 'company',
         'accounts': 'company', 'quote': 'quote', 'quotes': 'quote',
         'rfq': 'rfq', 'rfqs': 'rfq'}


def resolve_context(sc, context):
    """{type, id, label, visible, lead_id, company_id} or None.

    Visibility is decided by the same helpers every list uses — scoped
    leads and opportunities, records.company_access for an account,
    records.may_view_quote / may_view_rfq for the documents — and a
    quote or RFQ becomes the lead or account it belongs to. A record the
    viewer may not see keeps no label: echoing its name back would be a
    disclosure made by the context chip itself.
    """
    if not isinstance(context, dict):
        return None
    kind = _KIND.get(str(context.get('type') or '').strip().lower())
    try:
        ident = int(str(context.get('id') or '').strip())
    except (TypeError, ValueError):
        return None
    if not kind or ident <= 0:
        return None

    from app import Company, Lead, Opportunity
    from app.access import records as rec_mod

    hidden = {'type': kind, 'id': ident, 'label': f'this {kind}',
              'visible': False, 'lead_id': None, 'company_id': None}
    try:
        if kind == 'lead':
            row = (scope_mod.leads(sc=sc).filter(Lead.id == ident)
                   .with_entities(Lead.id, Lead.company, Lead.company_id)
                   .first())
            if row is None:
                return hidden
            return {'type': 'lead', 'id': row.id, 'label': row.company,
                    'visible': True, 'lead_id': row.id,
                    'company_id': row.company_id}
        if kind == 'opportunity':
            row = (scope_mod.opportunities(sc=sc)
                   .filter(Opportunity.id == ident).first())
            if row is None:
                return hidden
            return {'type': 'opportunity', 'id': row.id,
                    'label': row.opp_number, 'visible': True,
                    'lead_id': row.lead_id, 'company_id': row.company_id}
        if kind == 'company':
            from app import db
            company = db.session.get(Company, ident)
            if company is None or rec_mod.company_access(
                    company, sc=sc) == rec_mod.ROUTING:
                return hidden
            return {'type': 'company', 'id': company.id,
                    'label': company.name, 'visible': True, 'lead_id': None,
                    'company_id': company.id}
        from app import db
        if kind == 'quote':
            from app.models.quote import Quote
            doc = db.session.get(Quote, ident)
            ok = rec_mod.may_view_quote(doc, sc=sc)
        else:
            from app.models.rfq import RFQ
            doc = db.session.get(RFQ, ident)
            ok = rec_mod.may_view_rfq(doc, sc=sc)
        if not ok:
            return hidden
        if doc.lead_id:
            lead_ctx = resolve_context(sc, {'type': 'lead', 'id': doc.lead_id})
            if lead_ctx and lead_ctx['visible']:
                return lead_ctx
        if doc.account_id:
            acct = resolve_context(sc, {'type': 'company',
                                        'id': doc.account_id})
            if acct and acct['visible']:
                return acct
        return hidden
    except Exception:
        _log_exception('context')
        return hidden


def _public_context(ctx):
    if not ctx:
        return None
    return {'type': ctx['type'], 'id': ctx['id'], 'label': ctx['label'],
            'visible': bool(ctx['visible'])}


#: Questions about "this" record, by what the record is. A value ending
#: in '*' needs the record's account (a lead's or an opportunity's).
_CONTEXT_RULES = [
    (r'\b(emails?|mail trail|thread|last message|what did (they|the '
     r'customer|the client) (say|ask|want))\b',
     {'lead': 'thread_summary'}),
    (r'\b(attachments?|boq|cargo list|enquiry sheet)\b',
     {'lead': 'attachment_contents'}),
    (r'\b(contacts?|decision.?makers?|point of contact|poc|spoc|'
     r'who (should i|do i) (speak|talk) to|who (is|are) (the |our )?'
     r'(customer|client))\b',
     {'lead': 'key_contacts', 'opportunity': 'key_contacts',
      'company': 'key_contacts'}),
    (r'\bwho (else )?(knows|has worked|has dealt|talks|is close)\b|'
     r'\brelationships?\b',
     {'lead': 'relationship_map*', 'opportunity': 'relationship_map*',
      'company': 'relationship_map'}),
    (r'\b(cross.?sell|up.?sell|what else can we (sell|offer))\b',
     {'lead': 'cross_sell_account*', 'opportunity': 'cross_sell_account*',
      'company': 'cross_sell_account'}),
    (r'\b(next|next steps?|what should i do|what now|what do i do|'
     r'next best action)\b',
     {'lead': 'next_best_action', 'opportunity': 'opportunity_risk',
      'company': 'account_health'}),
    (r'\b(health|healthy|risk|risky|at risk|danger)\b',
     {'lead': 'next_best_action', 'opportunity': 'opportunity_risk',
      'company': 'account_health'}),
    (r'\b(pipeline|open deals?|opportunit\w*)\b',
     {'company': 'account_pipeline', 'lead': 'account_pipeline*'}),
    (r'\b(last quote|quoted|quotes?|price|rate)\b',
     {'company': 'last_quote_for_account'}),
    (r'\b(who (owns|handles|manages)|owner|pic)\b',
     {'company': 'account_owner', 'lead': 'lead_360'}),
    (r'\b(summar\w*|brief|overview|tell me about|story|status|360|'
     r'what.?s (happening|going on)|where are we|details?|about)\b',
     {'lead': 'lead_360', 'opportunity': 'opportunity_risk',
      'company': 'account_360'}),
]


def _context_plan(question, sc, ctx):
    """A Plan for a question about the page's record, or None."""
    q = ' ' + (question or '').lower().strip() + ' '
    for rx, by_kind in _CONTEXT_RULES:
        if not re.search(rx, q):
            continue
        target = by_kind.get(ctx['type'])
        if not target:
            continue
        if not ctx['visible']:
            return _restricted_context(ctx, target.rstrip('*'))
        needs_account = target.endswith('*')
        key = target.rstrip('*')
        if key == 'account_360' and not sc.can('reports.accounts'):
            # The same account, answered by the intent this viewer holds.
            key = 'account_health'
        if needs_account:
            if not ctx.get('company_id'):
                continue
            params = {'account_id': ctx['company_id']}
        elif ctx['type'] == 'company':
            params = {'account_id': ctx['id']}
        elif ctx['type'] == 'opportunity':
            params = {'opportunity_id': ctx['id']}
        else:
            params = {'lead_id': ctx['id']}
        return Plan(key, params, resolution='context', context=ctx)
    return None


def _restricted_context(ctx, key=None):
    return Plan(key, {}, context=ctx, restricted=(
        f'The {ctx["type"]} this panel is open on is outside your CRM '
        f'access, so I cannot answer about it. Its owner can help, or an '
        f'administrator can grant access on the Access Control screen.'))


# ── §6.3 conversation memory ─────────────────────────────────────────
#
# Stored nowhere new. Every question is already a copilot_log row with
# the asker, the words and the intent that answered; a conversation is
# a run of those rows. The id handed to the panel is the first row's id,
# signed with the app's secret and bound to the asker — so it cannot be
# forged into someone else's conversation, and even a forged one reads
# only rows carrying the caller's own emp_code.

def _secret():
    try:
        from flask import current_app
        key = current_app.secret_key
    except Exception:
        return None
    if not key:
        return None
    return key.encode() if isinstance(key, str) else key


def conversation_token(actor, start_id):
    key = _secret()
    if not key or not actor or not start_id:
        return None
    sig = hmac.new(key, f'copilot:{actor}:{int(start_id)}'.encode(),
                   hashlib.sha256).hexdigest()[:24]
    return f'c{int(start_id)}.{sig}'


def _conversation_start(token, actor):
    """The first log id of a conversation this actor owns, or None."""
    m = re.fullmatch(r'c(\d{1,12})\.([0-9a-f]{24})', str(token or ''))
    if not m or not actor:
        return None
    expected = conversation_token(actor, int(m.group(1)))
    if not expected or not hmac.compare_digest(expected, m.group(0)):
        return None
    return int(m.group(1))


def _conversation_after(actor, start, log_id):
    """The id to hand back: the same conversation, or a new one starting
    at this question."""
    if start:
        return conversation_token(actor, start)
    return conversation_token(actor, log_id) if log_id else None


def _memory(actor, start):
    """This actor's recent turns in this conversation, oldest first."""
    if not actor or not start:
        return []
    try:
        from app.models.copilot import CopilotLog

        since = datetime.utcnow() - timedelta(minutes=MEMORY_MINUTES)
        rows = (CopilotLog.query
                .filter(CopilotLog.emp_code == actor,
                        CopilotLog.id >= start,
                        CopilotLog.created_at >= since,
                        CopilotLog.intent.isnot(None),
                        CopilotLog.intent != '',
                        ~CopilotLog.intent.like('action:%'))
                .order_by(CopilotLog.id.desc()).limit(MEMORY_TURNS).all())
    except Exception:
        _log_exception('memory')
        return []
    return [{'question': r.question, 'intent': r.intent}
            for r in reversed(rows)]


def _memory_state(memory):
    """(intent, params) the conversation stands at after its last turn.

    Replayed from the stored words, deterministically: a turn that the
    rules read the same way again contributes its own params; a turn
    that was itself a follow-up is re-applied to the state before it;
    anything else (a model-routed turn) contributes its intent alone.
    """
    state = None
    for turn in memory:
        key, params, _how = _match_rules(turn['question'])
        if key == turn['intent']:
            state = (key, params)
            continue
        if state:
            plan = _follow_up(turn['question'], state, None)
            if plan is not None and plan.key == turn['intent']:
                state = (plan.key, plan.params)
                continue
        state = (turn['intent'], {})
    return state


_FOLLOW_LEAD = re.compile(
    r"^\s*(?:and|what about|how about|what if|same (?:for|but|thing for)|"
    r"only|just|now|then|ok(?:ay)?|also|but|for|in|over|during|"
    r"show(?: me)?(?: only| just)?|filter(?: to| by)?|narrow(?: it)? to|"
    r"make it|instead|switch to|change (?:it )?to)\b", re.IGNORECASE)

#: The intent that answers the same question about one account, for a
#: follow-up like "and for Acme?" after an answer that has no account.
ACCOUNT_VARIANT = {
    'pipeline_value': 'account_pipeline',
    'top_opportunities': 'account_pipeline',
    'pipeline_by_stage': 'account_pipeline',
    'closing_this_month': 'account_pipeline',
    'stalled_deals': 'account_pipeline',
    'opportunity_risk': 'account_pipeline',
    'opps_overdue_close': 'account_pipeline',
    'pipeline_health': 'account_health',
    'accounts_inactive': 'account_health',
    'top_accounts': 'account_health',
    'cross_sell_gap': 'cross_sell_account',
    'quotes_recent': 'last_quote_for_account',
    'quotes_above': 'last_quote_for_account',
    'quotes_awaiting_reply': 'last_quote_for_account',
    'leads_open': 'universal_search',
    'leads_new': 'universal_search',
    'leads_stale': 'universal_search',
}


def _follow_up(question, state, sc):
    """A Plan re-using the previous intent with modified params, or None.

    Recognised modifiers: a window ("last month"), a service ("only
    Project Freight"), an amount ("over 1 crore"), "overdue",
    "weighted", "by owner", and an account ("and for Acme?"). A
    modifier the previous intent cannot take is not silently dropped —
    the answer says it was not applied.
    """
    key, prev = state
    intent = catalogue.get(key)
    if intent is None:
        return None
    text = (question or '').strip()
    low = ' ' + text.lower() + ' '
    lead = _FOLLOW_LEAD.match(text)
    words = text.split()
    if not lead and len(words) > 6:
        return None

    declared = intent.params
    params = {k: v for k, v in prev.items()
              if k not in ('context', 'lead_id', 'opportunity_id')}
    applied, notes = [], []
    rest = text[lead.end():] if lead else text

    window = window_days(low)
    if window is None:
        days = _DAYS.search(low)
        window = int(days.group(1)) if days else None
    if window:
        rest = _WINDOW.sub(' ', rest)
        rest = re.sub(r'\b\d+\s*days?\b', ' ', rest, flags=re.IGNORECASE)
        if 'days' in declared:
            params['days'] = window
            applied.append('window')
        else:
            notes.append(f'“{intent.label}” has no date window, so the '
                         f'period you asked for was not applied.')
    service = vocabulary.service_in_text(low)
    if service:
        for phrase, _svc in vocabulary._SERVICE_WORDS:
            rest = re.sub(r'\b' + re.escape(phrase) + r'\b', ' ', rest,
                          flags=re.IGNORECASE)
        if 'vertical' in declared:
            params['vertical'] = service
            applied.append('service')
        else:
            notes.append(f'“{intent.label}” cannot be narrowed to one '
                         f'service, so {service} was not applied.')
    amount = parse_amount(text) if re.search(
        r'\b(above|over|more than|greater than|below|under)\b|\d\s*(cr|crore|'
        r'lakh|lac|l\b|k\b)', low) else None
    if amount:
        rest = _AMOUNT.sub(' ', rest)
        rest = re.sub(r'\b(above|over|more than|greater than)\b', ' ', rest,
                      flags=re.IGNORECASE)
        if 'amount' in declared:
            params['amount'] = amount
            applied.append('amount')
    if re.search(r'\boverdue\b', low) and 'overdue' in declared:
        params['overdue'] = 'true'
        applied.append('overdue')
        rest = re.sub(r'\boverdue( only)?\b', ' ', rest, flags=re.IGNORECASE)
    if re.search(r'\bweighted\b', low) and 'weighted' in declared:
        params['weighted'] = 'true'
        applied.append('weighted')
        rest = re.sub(r'\bweighted\b', ' ', rest, flags=re.IGNORECASE)
    by = re.search(r'\bby (stage|vertical|owner|city|customer)\b', low)
    if by and 'by' in declared:
        params['by'] = by.group(1)
        applied.append('by')
        rest = rest.replace(by.group(0), ' ')

    name = None
    rest = re.sub(r'^\s*(?:for|about|with|at|on|to)\s+', '', rest.strip(),
                  flags=re.IGNORECASE)
    rest = re.sub(r'\b(?:only|just|instead|please|then|too|as well)\b', ' ',
                  rest, flags=re.IGNORECASE)
    candidate = ' '.join(rest.split()).strip(' ?.!,')
    if candidate and any(w[:1].isupper() for w in candidate.split()):
        name = _clean_name(candidate)
    if name:
        if 'account' in declared:
            params['account'] = name
            params.pop('account_id', None)
            applied.append('account')
        elif key in ACCOUNT_VARIANT:
            key = ACCOUNT_VARIANT[key]
            params = ({'term': name} if key == 'universal_search'
                      else {'account': name})
            applied.append('account')
        else:
            return Plan(clarification={
                'question': f'What would you like to know about {name}?',
                'options': [
                    {'label': 'Account health',
                     'question': f'account health for {name}'},
                    {'label': 'Open deals',
                     'question': f'open deals for {name}'},
                    {'label': 'Who handles it',
                     'question': f'who handles {name}'}]})

    if not applied and not notes:
        return None
    return Plan(key, params, resolution='memory', notes=notes)


# ── §6.3 clarification for questions too broad to guess at ───────────
_AMBIGUOUS = [
    (r'^\s*(quotes?|quotations?)\s*\??\s*$', 'Which quotes do you mean?',
     [('Pending approval', 'quotes pending approval'),
      ('Awaiting a customer reply', 'quotes awaiting a reply'),
      ('Expiring soon', 'quotes expiring this week'),
      ('By status', 'quotes by status')]),
    (r'^\s*rfqs?\s*\??\s*$', 'Which RFQs do you mean?',
     [('Not yet quoted', 'unquoted RFQs'),
      ('Received this week', 'RFQs received this week'),
      ('By status', 'RFQs by status')]),
    (r'^\s*leads?\s*\??\s*$', 'Which leads do you mean?',
     [('Open', 'my open leads'), ('New this week', 'new leads this week'),
      ('Gone quiet', 'stale leads'), ('By stage', 'leads by stage')]),
    (r'^\s*(deals?|opportunit(y|ies))\s*\??\s*$',
     'Which opportunities do you mean?',
     [('Biggest', 'biggest deals'),
      ('At risk', 'which deals are at risk'),
      ('Stalled', 'stalled opportunities'),
      ('Closing this month', 'closing this month')]),
    (r'^\s*(accounts?|customers?|clients?)\s*\??\s*$',
     'Which accounts do you mean?',
     [('Top accounts', 'top accounts'),
      ('Gone quiet', 'inactive accounts'),
      ('Health', 'account health'),
      ('No owner', 'accounts with no owner')]),
    (r'^\s*(health|health ?check|how healthy)\s*\??\s*$',
     'The health of what?',
     [('Pipeline', 'pipeline health'), ('Accounts', 'account health'),
      ('Customers', 'customer health')]),
    (r'^\s*(risks?|at risk|what is at risk)\s*\??\s*$', 'Which risk?',
     [('Opportunities at risk', 'which deals are at risk'),
      ('Pipeline health', 'pipeline health'),
      ('Accounts at risk', 'which accounts are at risk')]),
    (r'^\s*(status|update|summary|overview|report)\s*\??\s*$',
     'A summary of what?',
     [('My day', 'my day'), ('Pipeline', 'pipeline value'),
      ('Today in sales', 'what happened in sales today')]),
    (r'^\s*(performance|numbers|stats|kpis?)\s*\??\s*$', 'Whose numbers?',
     [('Mine', 'my numbers'), ('My win rate', 'my win rate'),
      ('Win rate by vertical', 'win rate by vertical')]),
    (r'^\s*(follow.?ups?|tasks?|to.?dos?)\s*\??\s*$', 'Which do you mean?',
     [('Follow-ups due', 'which follow-ups are due'),
      ('My open tasks', 'my open tasks'),
      ('Leads with no next action', 'leads with no next action')]),
]


def _ambiguous(question, sc):
    """A clarification with 2–4 options the viewer is entitled to, or
    None. Each option is a full question that routes on its own."""
    q = (question or '').strip().lower()
    for rx, prompt, options in _AMBIGUOUS:
        if not re.search(rx, q):
            continue
        kept = []
        for label, text in options:
            key, _params, _how = _match_rules(text)
            intent = catalogue.get(key) if key else None
            if intent and (not intent.permission
                           or sc is None or sc.can(intent.permission)):
                kept.append({'label': label, 'question': text})
        if len(kept) >= 2:
            return {'question': prompt, 'options': kept[:4]}
    return None


# ── what to ask next ─────────────────────────────────────────────────
#: 2–3 questions worth asking after each intent. Each is a full question
#: that the rules route to the intent named beside it — the library test
#: checks every one — and {account} is filled only when the answer was
#: about a single account.
FOLLOW_UP_CHIPS = {
    'my_day': [('which follow-ups are due', 'followups_due'),
               ('who should I call', 'next_best_action'),
               ('my open tasks', 'my_tasks')],
    'followups_due': [('who should I call', 'next_best_action'),
                      ('leads with no next action', 'leads_no_next_action'),
                      ('stale leads', 'leads_stale')],
    'leads_open': [('stale leads', 'leads_stale'),
                   ('leads with no next action', 'leads_no_next_action'),
                   ('leads by stage', 'leads_by_stage')],
    'leads_stale': [('who should I call', 'next_best_action'),
                    ('which follow-ups are due', 'followups_due'),
                    ('leads with no next action', 'leads_no_next_action')],
    'leads_no_next_action': [('stale leads', 'leads_stale'),
                             ('which follow-ups are due', 'followups_due')],
    'leads_new': [('lead sources', 'lead_sources'),
                  ('unassigned leads', 'leads_unassigned'),
                  ('leads by stage', 'leads_by_stage')],
    'leads_by_stage': [('new leads this week', 'leads_new'),
                       ('pipeline value', 'pipeline_value')],
    'lead_sources': [('new leads this week', 'leads_new'),
                     ('win rate by vertical', 'win_rate_by_vertical')],
    'leads_unassigned': [('accounts with no owner', 'dq_accounts_no_owner'),
                         ('data quality', 'dq_missing_fields')],
    'rfqs_unquoted': [('overdue RFQs', 'rfqs_unquoted'),
                      ('RFQs received this week', 'rfqs_recent'),
                      ('quotation turnaround time', 'quote_turnaround')],
    'rfqs_recent': [('unquoted RFQs', 'rfqs_unquoted'),
                    ('RFQs by status', 'rfqs_by_status')],
    'rfqs_by_status': [('unquoted RFQs', 'rfqs_unquoted'),
                       ('quotes by status', 'quotes_by_status')],
    'rfqs_high_value_recent': [('unquoted RFQs', 'rfqs_unquoted'),
                               ('RFQs by status', 'rfqs_by_status')],
    'quotes_awaiting_reply': [('quotes expiring this week',
                               'quotes_expiring'),
                              ('quotes pending approval',
                               'quotes_pending_approval')],
    'quotes_pending_approval': [('quotes expiring this week',
                                 'quotes_expiring'),
                                ('quotes by status', 'quotes_by_status')],
    'quotes_expiring': [('quotes awaiting a reply', 'quotes_awaiting_reply'),
                        ('quotes sent this week', 'quotes_recent')],
    'quotes_recent': [('quotes awaiting a reply', 'quotes_awaiting_reply'),
                      ('quotes by status', 'quotes_by_status')],
    'quotes_by_status': [('quotes pending approval',
                          'quotes_pending_approval'),
                         ('quotes expiring this week', 'quotes_expiring')],
    'quotes_above': [('quotes awaiting a reply', 'quotes_awaiting_reply'),
                     ('quotes expiring this week', 'quotes_expiring')],
    'last_quote_for_account': [('open deals for {account}',
                                'account_pipeline'),
                               ('who handles {account}', 'account_owner')],
    'quote_turnaround': [('unquoted RFQs', 'rfqs_unquoted'),
                         ('quotes by status', 'quotes_by_status')],
    'pipeline_value': [('pipeline health', 'pipeline_health'),
                       ('pipeline by stage', 'pipeline_by_stage'),
                       ('which deals are at risk', 'opportunity_risk')],
    'pipeline_by_stage': [('pipeline health', 'pipeline_health'),
                          ('biggest deals', 'top_opportunities')],
    'pipeline_health': [('which deals are at risk', 'opportunity_risk'),
                        ('opportunities past their close date',
                         'opps_overdue_close'),
                        ('stalled opportunities', 'stalled_deals')],
    'top_opportunities': [('which deals are at risk', 'opportunity_risk'),
                          ('closing this month', 'closing_this_month')],
    'stalled_deals': [('which deals are at risk', 'opportunity_risk'),
                      ('pipeline health', 'pipeline_health')],
    'opportunity_risk': [('opportunities past their close date',
                          'opps_overdue_close'),
                         ('stalled opportunities', 'stalled_deals'),
                         ('pipeline health', 'pipeline_health')],
    'opps_overdue_close': [('which deals are at risk', 'opportunity_risk'),
                           ('closing this month', 'closing_this_month')],
    'closing_this_month': [('which deals are at risk', 'opportunity_risk'),
                           ('weighted pipeline', 'pipeline_value')],
    'account_pipeline': [('account health for {account}', 'account_health'),
                         ('who knows {account}', 'relationship_map'),
                         ('cross sell for {account}', 'cross_sell_account')],
    'account_owner': [('account health for {account}', 'account_health'),
                      ('open deals for {account}', 'account_pipeline')],
    'account_status': [('who handles {account}', 'account_owner'),
                       ('who knows {account}', 'relationship_map')],
    'account_360': [('account health for {account}', 'account_health'),
                    ('key contacts for {account}', 'key_contacts'),
                    ('cross sell for {account}', 'cross_sell_account')],
    'account_health': [('who knows {account}', 'relationship_map'),
                       ('cross sell for {account}', 'cross_sell_account'),
                       ('open deals for {account}', 'account_pipeline')],
    'cross_sell_account': [('key contacts for {account}', 'key_contacts'),
                           ('account health for {account}',
                            'account_health')],
    'relationship_map': [('key contacts for {account}', 'key_contacts'),
                         ('account health for {account}', 'account_health')],
    'key_contacts': [('who knows {account}', 'relationship_map'),
                     ('open deals for {account}', 'account_pipeline')],
    'accounts_inactive': [('account health', 'account_health'),
                          ('top accounts', 'top_accounts')],
    'top_accounts': [('customer health', 'account_health'),
                     ('cross sell opportunities', 'cross_sell_gap')],
    'cross_sell_gap': [('top accounts', 'top_accounts'),
                       ('customer health', 'account_health')],
    'handovers_recent': [('won deals awaiting PO', 'handovers_awaiting_po'),
                         ('won deals with no PO', 'handover_missing_po')],
    'handovers_awaiting_po': [('won deals with no PO',
                               'handover_missing_po'),
                              ('recent handovers to operations',
                               'handovers_recent')],
    'handover_missing_po': [('won deals awaiting PO',
                             'handovers_awaiting_po'),
                            ('recent wins', 'deals_won_recent')],
    'deals_won_recent': [('won deals awaiting PO', 'handovers_awaiting_po'),
                         ('win rate by vertical', 'win_rate_by_vertical')],
    'deals_lost_recent': [('top loss reasons', 'loss_analysis'),
                          ('lost leads with no reason', 'dq_lost_no_reason')],
    'loss_analysis': [('recent losses', 'deals_lost_recent'),
                      ('win rate by vertical', 'win_rate_by_vertical')],
    'conversion_rate': [('win rate by vertical', 'win_rate_by_vertical'),
                        ('my win rate', 'my_win_rate')],
    'win_rate_by_vertical': [('where are we losing', 'loss_analysis'),
                             ('recent wins', 'deals_won_recent')],
    'my_performance': [('my win rate', 'my_win_rate'),
                       ('who should I call', 'next_best_action')],
    'my_win_rate': [('my numbers', 'my_performance'),
                    ('which deals are at risk', 'opportunity_risk')],
    'team_activity_gap': [('team workload', 'team_workload'),
                          ('what happened in sales today', 'daily_digest')],
    'team_workload': [("who hasn't updated CRM this week",
                       'team_activity_gap'),
                      ('unassigned leads', 'leads_unassigned')],
    'daily_digest': [('recent wins', 'deals_won_recent'),
                     ('new leads this week', 'leads_new')],
    'dq_missing_fields': [('accounts with no owner', 'dq_accounts_no_owner'),
                          ('are there duplicate accounts', 'dq_duplicates'),
                          ('lost leads with no reason', 'dq_lost_no_reason')],
    'dq_duplicates': [('accounts with no owner', 'dq_accounts_no_owner'),
                      ('data quality', 'dq_missing_fields')],
    'dq_lost_no_reason': [('data quality', 'dq_missing_fields'),
                          ('top loss reasons', 'loss_analysis')],
    'dq_accounts_no_owner': [('unassigned leads', 'leads_unassigned'),
                             ('data quality', 'dq_missing_fields')],
    'intake_review_pending': [('unassigned leads', 'leads_unassigned'),
                              ('new leads this week', 'leads_new')],
    'my_tasks': [('which follow-ups are due', 'followups_due'),
                 ('my day', 'my_day')],
    'next_best_action': [('which follow-ups are due', 'followups_due'),
                         ('quotes awaiting a reply', 'quotes_awaiting_reply')],
    'lead_360': [('what did the customer ask in the latest email',
                  'thread_summary'),
                 ('what should I do next', 'next_best_action')],
    'thread_summary': [('what is in the attachment', 'attachment_contents'),
                       ('what should I do next', 'next_best_action')],
    'attachment_contents': [('summarise the email thread', 'thread_summary')],
    'universal_search': [('my open leads', 'leads_open'),
                         ('my day', 'my_day')],
    'search_text': [('my open leads', 'leads_open')],
}


def follow_ups(key, params, result, sc):
    """Up to three next questions, each one this viewer may ask."""
    account = ((result.filters or {}).get('account')
               or params.get('account') or '')
    out = []
    for template, target in FOLLOW_UP_CHIPS.get(key, []):
        if '{account}' in template:
            if not account:
                continue
            text = template.format(account=account)
        else:
            text = template
        intent = catalogue.get(target)
        if intent is None or (intent.permission
                              and not sc.can(intent.permission)):
            continue
        if text not in out:
            out.append(text)
        if len(out) == 3:
            break
    return out


# ── how the answer was reached, in words ─────────────────────────────
_VIA = {'pattern': 'matched your wording',
        'synonym': 'matched through a synonym of your wording',
        'context': 'about the record this panel is open on',
        'memory': 'a follow-up to your previous question',
        'model': 'interpreted by the AI model'}


def how_answered(intent, params, result, plan, sc):
    """"Answered with “Stale leads” · idle 14+ days · service: Project
    Freight · matched your wording · your own records". Never a query."""
    from app.copilot.queries import _money
    from app.models.access import DataScope

    bits = []
    shown = dict(params or {})
    shown.update({k: v for k, v in (result.filters or {}).items()
                  if v not in (None, '')})
    if shown.get('days'):
        bits.append(f'last {shown["days"]} day(s)')
    if shown.get('amount'):
        try:
            bits.append(f'above {_money(float(shown["amount"]))}')
        except (TypeError, ValueError):
            pass
    if shown.get('account'):
        bits.append(f'account: {shown["account"]}')
    elif shown.get('account_id'):
        bits.append('the account in context')
    if shown.get('vertical'):
        bits.append(f'service: {shown["vertical"]}')
    if shown.get('by'):
        bits.append(f'by {shown["by"]}')
    if str(shown.get('weighted') or '').lower() in ('true', '1', 'yes'):
        bits.append('weighted by probability')
    if shown.get('overdue'):
        bits.append('overdue only')
    if shown.get('segment'):
        bits.append(f'{shown["segment"]} only')
    if shown.get('term'):
        bits.append(f'search: “{str(shown["term"])[:60]}”')
    if shown.get('source'):
        bits.append(f'in {shown["source"]}s')
    if shown.get('date_from'):
        bits.append(f'since {shown["date_from"]}')
    if shown.get('date_to'):
        bits.append(f'until {shown["date_to"]}')
    if shown.get('owner'):
        bits.append(f'owner: {shown["owner"]}')
    if shown.get('lead_id') and not shown.get('account'):
        bits.append('the lead in context')
    if shown.get('opportunity_id') or shown.get('opportunity'):
        bits.append('one opportunity')
    via = _VIA.get(plan.resolution)
    if via:
        bits.append(via)
    scope = ('the whole company' if sc.data_scope == DataScope.ALL else
             'your vertical' if sc.data_scope == DataScope.VERTICAL else
             'your own records')
    bits.append(scope)
    return f'Answered with “{intent.label}” · ' + ' · '.join(bits)


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
            answer=((answer.result.headline if answer.result else '')
                    or answer.prose or '')[:500],
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


def _feedback_reason(text, note):
    """The panel asks "what was wrong?" in free text and lists the
    reasons in lower case. Only exact labels used to be accepted, so
    every typed answer was refused with a 400 the panel never showed —
    thumbs-down votes were silently lost. A matching phrase maps to its
    label; anything else is kept, verbatim, as the note under Other."""
    t = (text or '').strip()
    for label in FEEDBACK_REASONS:
        if t.lower() == label.lower() or label.lower() in t.lower():
            return label, note
    return 'Other', (note or t)


def record_feedback(log_id, *, helpful, reason=None, note=None, actor=None):
    from app import db
    from app.models.copilot import CopilotLog

    try:
        row = db.session.get(CopilotLog, int(log_id))
    except (TypeError, ValueError):
        row = None
    # Only on your own answer, like pin. Otherwise anyone could mark any
    # answer unhelpful and skew the analytics page.
    if row is None or (actor and row.emp_code and row.emp_code != actor):
        return False, 'Not found'
    if not helpful and reason:
        reason, note = _feedback_reason(reason, note)
    row.helpful = bool(helpful)
    row.feedback_reason = (reason or '')[:60] or None
    row.feedback_note = (note or '')[:500] or None
    row.feedback_by = actor or ''
    db.session.commit()
    return True, None


def pin(log_id, *, pinned, actor=None):
    """§6.4 — save a question, not its answer.

    Pinning the rows would freeze last month's pipeline into something
    that still looks authoritative. Pinning the question means opening
    it re-runs it, against today's data and today's permissions.
    """
    from app import db
    from app.models.copilot import CopilotLog

    row = db.session.get(CopilotLog, int(log_id))
    if row is None:
        return False, 'Not found'
    if actor and row.emp_code and row.emp_code != actor:
        return False, 'That is not your question.'
    row.pinned = bool(pinned)
    row.pinned_at = datetime.utcnow() if pinned else None
    db.session.commit()
    return True, None


def pinned_for(actor, *, limit=20):
    from app.models.copilot import CopilotLog

    rows = (CopilotLog.query
            .filter(CopilotLog.emp_code == actor,
                    CopilotLog.pinned.is_(True))
            .order_by(CopilotLog.pinned_at.desc()).limit(limit).all())
    return [{'id': r.id, 'question': r.question, 'intent': r.intent or ''}
            for r in rows]


def morning_brief(sc):
    """§6.5 — the proactive greeting, from the same intent as My Day so
    the two can never disagree."""
    intent = catalogue.get('my_day')
    if intent is None:
        return None
    result = intent.handler(sc, {})
    return {'headline': result.headline, 'figures': result.figures,
            'empty': result.empty}


def suggestions(sc, *, limit=10):
    """§6.2 — the chips, role-aware, so none of them can refuse."""
    return [{'key': i.key, 'label': i.label,
             'example': i.examples[0] if i.examples else i.label}
            for i in catalogue.available(sc)][:limit]
