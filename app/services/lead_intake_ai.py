"""
Phase 4 — a model's opinion, where the rules are unsure.

The rules already decide 97% of the mailbox. This is for the rest: the
band where the confidence score lands between "probably not" and
"probably yes" and a person would otherwise have to look.

What it is not allowed to do
    It is never consulted before step 10. A thread match, a Procam
    sender, a known vendor and the logistics gate all decide without it,
    because those are facts and this is a guess. It cannot name a lead to
    attach to, cannot invent an account, and cannot overturn anything
    deterministic — the worst it can do is move one uncertain email
    between New Lead and Needs Review.

    Every opinion is recorded next to the rule's own answer, so the two
    can be compared later. If the model turns out no better than the
    score it is replacing, that will be visible rather than assumed.

Failure is not an error
    No key, no budget, a timeout, malformed JSON, an unknown class — all
    return None and the rules stand. An intake path that breaks when a
    third party is slow would be a worse system than one with no model
    at all.
"""
from __future__ import annotations

import json
import os
import re

from app.services import lead_intake as li


#: Only these two outcomes are the model's to influence. It is answering
#: "is this a new enquiry?", not re-running the whole tree.
_ALLOWED = (li.Klass.NEW_LEAD, li.Klass.REVIEW, li.Klass.NON_BUSINESS,
            li.Klass.INTERNAL, li.Klass.RATE_SOURCING, li.Klass.QUOTE)

#: The band where a rule is worth a second opinion. Outside it the score
#: is already decisive and spending a call would buy nothing.
CONSULT_BELOW = 80
CONSULT_ABOVE = 25

_TIMEOUT = float(os.environ.get('LEAD_INTAKE_AI_TIMEOUT', '12'))
_MAX_BODY = 4000
#: Enough that a reason cannot be cut off mid-JSON, which Groq
#: rejects as a malformed request rather than returning.
_MAX_TOKENS = 400

_PROMPT = """You classify emails arriving at a freight and project-logistics \
company's shared enquiry mailbox.

Decide what THIS email is. Answer with JSON only:
{"classification": "...", "confidence": 0-100, "reason": "under 20 words"}

classification must be exactly one of:
  new_lead       a customer asking us to quote or move something — a new
                 enquiry we could win work from
  needs_review   genuinely ambiguous; a person should look
  non_business   newsletters, marketing, alerts, notifications, nothing
                 to sell against
  internal       correspondence between our own staff
  rate_sourcing  us asking a supplier for a price, or a supplier quoting us
  quote_submission  us sending a price to a customer

Guidance:
- Most mail here is forwarded in by our own staff. Judge the ORIGINAL
  sender and the enquiry inside, not the colleague who relayed it.
- A customer writing "send us your best rate" is asking us to quote:
  that is a new enquiry, not rate sourcing.
- An automated tender or auction notification is not a new enquiry
  unless it names work we could bid for.
- If you are unsure, answer needs_review. A missed enquiry costs far
  more than a duplicate one."""

_MAP = {
    'new_lead': li.Klass.NEW_LEAD,
    'needs_review': li.Klass.REVIEW,
    'non_business': li.Klass.NON_BUSINESS,
    'internal': li.Klass.INTERNAL,
    'rate_sourcing': li.Klass.RATE_SOURCING,
    'quote_submission': li.Klass.QUOTE,
}


class Opinion:
    def __init__(self, klass, confidence, reason, model=None):
        self.klass = klass
        self.confidence = confidence
        self.reason = reason
        self.model = model

    def to_dict(self):
        return {'ai_class': self.klass, 'ai_confidence': self.confidence,
                'ai_reason': self.reason, 'ai_model': self.model}

    def __repr__(self):
        return f'<Opinion {self.klass} {self.confidence}%>'


def is_enabled():
    """Off unless switched on and a key exists.

    Two conditions rather than one: the flag is the business decision,
    the key is whether it can work at all.
    """
    if (os.environ.get('LEAD_INTAKE_AI') or 'off').lower() not in (
            'on', 'true', '1', 'yes'):
        return False
    return bool(os.environ.get('GROQ_API_KEY')
                or os.environ.get('ANTHROPIC_API_KEY'))


def should_consult(rule_decision):
    """Whether this decision is worth a call.

    Only step 10 — everything above it is deterministic and a model has
    nothing to add to a thread match.
    """
    if rule_decision is None or rule_decision.step != 10:
        return False
    score = rule_decision.confidence
    if score is None:
        return False
    return CONSULT_ABOVE <= score < CONSULT_BELOW


def opinion(msg, rule_decision=None):
    """The model's read, or None. Never raises."""
    if not is_enabled():
        return None
    if rule_decision is not None and not should_consult(rule_decision):
        return None
    try:
        from email_ingest import ai_budget
        if not ai_budget.can_spend(2000):
            return None
    except Exception:
        pass

    try:
        text = _as_prompt(msg)
        if not text.strip():
            return None
        raw, model = _ask(text)
        if not raw:
            return None
        parsed = _parse(raw)
        if parsed is None:
            return None
        parsed.model = model
        return parsed
    except Exception as exc:
        # Silent to the caller, not to the log. The rules already have an
        # answer and an intake path that fails when a third party is slow
        # would be worse than one with no model — but a failure nobody
        # can diagnose is how a permanently broken model goes unnoticed.
        try:
            from app import app as flask_app
            flask_app.logger.info(
                'intake AI unavailable, rules stand: %s: %s',
                type(exc).__name__, str(exc)[:300])
        except Exception:
            pass
        return None


def _as_prompt(msg):
    parts = [
        f"From: {msg.get('_resolved_sender') or li.sender(msg)}",
        f"Forwarded by: {li.sender(msg)}"
        if msg.get('_forward_resolved') else '',
        f"To: {', '.join(li.recipients(msg, 'toRecipients')[:5])}",
        f"Subject: {msg.get('subject') or ''}",
    ]
    names = li.attachment_names(msg)
    if names:
        parts.append(f"Attachments: {', '.join(names[:8])}")
    parts.append('')
    parts.append((li.body_text(msg) or '')[:_MAX_BODY])
    return '\n'.join(p for p in parts if p)


def _ask(text):
    """Groq first — it is cheap and the key exists. Anthropic if not."""
    if os.environ.get('GROQ_API_KEY'):
        return _ask_groq(text)
    return _ask_anthropic(text)


def _ask_groq(text):
    from openai import OpenAI

    model = (os.environ.get('LEAD_INTAKE_AI_MODEL')
             or os.environ.get('EMAIL_AI_MODEL_GROQ')
             or 'llama-3.3-70b-versatile')
    client = OpenAI(api_key=os.environ['GROQ_API_KEY'],
                    base_url='https://api.groq.com/openai/v1',
                    timeout=_TIMEOUT)

    def call(strict):
        kw = {'response_format': {'type': 'json_object'}} if strict else {}
        resp = client.chat.completions.create(
            model=model, temperature=0, max_tokens=_MAX_TOKENS,
            messages=[{'role': 'system', 'content': _PROMPT},
                      {'role': 'user', 'content': text}],
            **kw)
        _record_spend(
            getattr(getattr(resp, 'usage', None), 'total_tokens', 0))
        return resp.choices[0].message.content

    try:
        return call(strict=True), model
    except Exception as exc:
        # Groq's json_object mode rejects the whole request when the
        # model's own output does not validate — a 400, not a bad
        # answer. Asking again without the constraint usually works,
        # and _parse() already digs the JSON out of prose. One retry:
        # a second failure is a real failure.
        if 'json_validate_failed' not in str(exc):
            raise
        return call(strict=False), model


def _ask_anthropic(text):
    import anthropic

    model = (os.environ.get('LEAD_INTAKE_AI_MODEL')
             or 'claude-haiku-4-5-20251001')
    client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'],
                                 timeout=_TIMEOUT)
    msg = client.messages.create(
        model=model, max_tokens=_MAX_TOKENS, temperature=0,
        system=_PROMPT,
        messages=[{'role': 'user', 'content': text}])
    out = ''.join(b.text for b in msg.content if hasattr(b, 'text'))
    usage = getattr(msg, 'usage', None)
    _record_spend((getattr(usage, 'input_tokens', 0) or 0)
                  + (getattr(usage, 'output_tokens', 0) or 0))
    return out, model


def _record_spend(tokens):
    try:
        from email_ingest import ai_budget
        ai_budget.record(int(tokens or 0))
    except Exception:
        pass


def _parse(raw):
    """Model output → Opinion, or None. Nothing here trusts the model."""
    m = re.search(r'\{[\s\S]*\}', raw or '')
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    klass = _MAP.get(str(data.get('classification') or '').strip().lower())
    if klass not in _ALLOWED:
        return None

    try:
        confidence = int(float(data.get('confidence') or 0))
    except (TypeError, ValueError):
        confidence = 0
    confidence = max(0, min(100, confidence))

    reason = str(data.get('reason') or '').strip()[:200]
    return Opinion(klass, confidence, reason or 'no reason given')


def apply(rule_decision, ai):
    """Fold an opinion into the rule's decision.

    The model only moves things within the uncertain band, and only when
    it is more sure than the rule was. It can promote a review to a lead
    or demote a marginal lead — nothing else. Anything deterministic is
    already returned before this is reached.
    """
    if ai is None or rule_decision is None:
        return rule_decision
    if not should_consult(rule_decision):
        return rule_decision
    # A model that is less certain than the score it is second-guessing
    # has not earned the override.
    if ai.confidence < 70:
        rule_decision.extra.update(ai.to_dict())
        rule_decision.extra['ai_applied'] = False
        return rule_decision

    rule_decision.extra.update(ai.to_dict())
    rule_decision.extra['ai_applied'] = True
    rule_decision.extra['rule_class'] = rule_decision.klass
    rule_decision.klass = ai.klass
    rule_decision.step = '10+ai'
    rule_decision.needs_review = (ai.klass == li.Klass.REVIEW)
    rule_decision.reason = (
        f'{rule_decision.reason}; model said {ai.klass} '
        f'({ai.confidence}%) — {ai.reason}')
    return rule_decision
