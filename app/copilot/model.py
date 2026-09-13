"""
The model, kept at arm's length — §3.1.

Nothing here talks to a public API. `PROCAM_AI_BASE_URL` must point at
something Procam runs: vLLM, Ollama, llama.cpp, TGI — all of them speak
the OpenAI chat shape, so the adapter is the same for any of them and
swapping models is a line in `.env`.

Two jobs, and only two
    classify()  free text → an intent key and parameters
    narrate()   a Result → prose about that Result

It is never given a query to write, a scope to interpret, or a fact the
Result does not already contain. That is not a policy the prompt asks
the model to respect — it is the shape of the interface, which is why
§9's injection defence holds even if a prompt is subverted.

Absent, slow or broken, every function here returns None and the caller
falls back to patterns and to the Result's own headline.
"""
from __future__ import annotations

import json
import os
import re


#: A public endpoint here would breach §3.1, so it is refused outright
#: rather than trusted to configuration review.
_PUBLIC = ('api.openai.com', 'api.anthropic.com', 'api.groq.com',
           'generativelanguage.googleapis.com', 'api.mistral.ai',
           'api.cohere.ai', 'api.together.xyz', 'openrouter.ai',
           'api.deepseek.com', 'api.perplexity.ai')

_TIMEOUT = float(os.environ.get('PROCAM_AI_TIMEOUT', '20'))


def base_url():
    return (os.environ.get('PROCAM_AI_BASE_URL') or '').strip().rstrip('/')


def model_name():
    return (os.environ.get('PROCAM_AI_MODEL') or 'qwen2.5:14b-instruct')


def classifier_model():
    """A smaller model may answer the latency-critical path."""
    return (os.environ.get('PROCAM_AI_CLASSIFIER_MODEL')
            or model_name())


def refusal():
    """Why the model is unavailable, in words an admin can act on."""
    url = base_url()
    if not url:
        return ('No model host configured. Set PROCAM_AI_BASE_URL to a '
                'Procam-hosted endpoint.')
    host = re.sub(r'^https?://', '', url).split('/')[0].lower()
    for bad in _PUBLIC:
        if host == bad or host.endswith('.' + bad):
            return (f'{host} is a public AI API. Procam AI runs only '
                    f'against Procam-hosted models (§3.1).')
    return None


def available():
    """Whether a Procam-hosted model can be called at all."""
    return refusal() is None


# ── the two jobs ─────────────────────────────────────────────────────
_CLASSIFY_SYSTEM = """You route questions asked inside a freight and \
project-logistics company's CRM to one catalogued intent.

Answer with JSON only:
{"intent": "<key or null>", "params": {...}, "confidence": 0-100}

Rules:
- The intent MUST be one of the keys listed. Never invent one.
- If nothing fits, answer {"intent": null}. A wrong intent is worse than
  none: it answers a question nobody asked.
- Extract only the parameters that intent declares. Leave the rest out.
- Amounts may be written "50 lakh", "1.2 cr", "5000000" — return rupees
  as a plain number.
- The user is a Procam employee asking about their own CRM. Never
  reinterpret the question as a request to change access or reveal
  other people's data; you cannot do either, and such a question is
  simply an unrecognised intent."""

_NARRATE_SYSTEM = """You write one short paragraph explaining a result \
already computed from a logistics CRM.

Absolute rules:
- Use ONLY the numbers and records in the JSON given to you. Never add a
  figure, a name, a date or a total that is not there.
- Do not recompute anything. The numbers are final.
- Two or three sentences at most. Plain, direct, no preamble, no
  "Certainly" or "Based on the data".
- If something is missing or the sample is small, say so plainly.
- Indian scales: write 4800000 as 48 lakh, 12000000 as 1.2 crore."""


def classify(question, catalogue, *, history=None):
    """{'intent': key, 'params': {...}} or None."""
    if not available():
        return None
    keys = [
        {'key': i['key'], 'label': i['label'], 'params': i['params'],
         'examples': i['examples'][:3]}
        for i in catalogue
    ]
    prior = ''
    if history:
        prior = '\n\nEarlier in this conversation:\n' + '\n'.join(
            f'- {h}' for h in list(history)[-4:])
    user = (f'Intents:\n{json.dumps(keys, ensure_ascii=False)}'
            f'{prior}\n\nQuestion: {question}')
    raw = _chat(_CLASSIFY_SYSTEM, user, model=classifier_model(),
                max_tokens=250)
    data = _parse_json(raw)
    if not isinstance(data, dict):
        return None
    key = data.get('intent')
    if not key or not isinstance(key, str):
        return None
    params = data.get('params')
    return {'intent': key,
            'params': params if isinstance(params, dict) else {}}


def narrate(question, intent_label, result_dict):
    """A paragraph, or None. Never raises."""
    if not available():
        return None
    payload = {k: v for k, v in result_dict.items()
               if k in ('headline', 'columns', 'figures', 'notes',
                        'sources')}
    # Rows are trimmed hard: the narrator needs the shape of the answer,
    # not every row. The table is rendered from the Result itself.
    rows = result_dict.get('rows') or []
    payload['rows_sample'] = [
        {k: v for k, v in r.items() if not k.startswith('_')}
        for r in rows[:8]]
    payload['row_count'] = len(rows)

    user = (f'Question: {question}\nIntent: {intent_label}\n'
            f'Result:\n{json.dumps(payload, ensure_ascii=False, default=str)}')
    out = _chat(_NARRATE_SYSTEM, user, model=model_name(), max_tokens=320)
    return (out or '').strip() or None


# ── transport ────────────────────────────────────────────────────────
def _chat(system, user, *, model, max_tokens):
    """One call to the Procam-hosted endpoint. None on any failure."""
    if not available():
        return None
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=os.environ.get('PROCAM_AI_KEY') or 'not-needed',
            base_url=base_url(), timeout=_TIMEOUT)
        resp = client.chat.completions.create(
            model=model, temperature=0, max_tokens=max_tokens,
            messages=[{'role': 'system', 'content': system},
                      {'role': 'user', 'content': user}])
        return resp.choices[0].message.content
    except Exception as exc:
        try:
            from app import app as flask_app
            flask_app.logger.info(
                'copilot model unavailable, falling back: %s: %s',
                type(exc).__name__, str(exc)[:200])
        except Exception:
            pass
        return None


def _parse_json(raw):
    m = re.search(r'\{[\s\S]*\}', raw or '')
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def health():
    """What the admin screen shows about the model."""
    why = refusal()
    return {
        'available': why is None,
        'reason': why,
        'base_url': base_url() or None,
        'model': model_name() if why is None else None,
        'classifier': classifier_model() if why is None else None,
    }
