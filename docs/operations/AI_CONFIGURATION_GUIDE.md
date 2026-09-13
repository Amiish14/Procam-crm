# AI Configuration Guide

The CRM uses AI in four places. They differ in one way that matters most:
**where the data goes.**

| Feature | Provider | CRM data leaves Procam? | Default |
|---|---|---|---|
| Procam AI Copilot — answers | none needed; optional self-hosted model | **No**, public AI hosts are refused | deterministic answers, no model |
| Copilot — semantic search | optional self-hosted embedder | **No** | lexical (keyword) search |
| Email intake — extraction and the classifier's AI step | Groq (primary), Anthropic (fallback) | **Yes** — email text | extraction on if `GROQ_API_KEY` is set; classifier AI step `off` |
| AI Outreach drafts, business-card OCR | Anthropic | **Yes** — lead context, card images | on if `ANTHROPIC_API_KEY` is set |

The Copilot was built to the rule "never expose CRM data to a public AI
API". The intake and outreach features predate it and do send data to
public providers. **Whether that continues is a business decision** — see
"Decision required" below. Nothing in this guide changes it by itself.

Check what a server is actually doing:

```bash
.venv/bin/python scripts/production_preflight.py | sed -n '/CONFIG/,/DATABASE/p'
```

It FAILs a Copilot host that is not provably private, and WARNs naming
every public provider in use. It never prints a key.

---

## Procam AI Copilot

Works with **no configuration**: 37 catalogued questions (my open leads,
follow-ups due, pipeline by stage, stale accounts, search the notes and
emails …) answered straight from the database, confined to what the
asker's Access Matrix profile allows. The model never writes a query; it
can only pick one of the catalogued ones.

### Optional: a self-hosted model

Lets people phrase questions freely and get a written summary of the
rows. Run an OpenAI-compatible server (Ollama, vLLM, llama.cpp) on
Procam's network, then in `.env`:

```bash
PROCAM_AI_BASE_URL=http://10.x.x.x:11434/v1    # private address only
PROCAM_AI_MODEL=qwen2.5:14b-instruct
PROCAM_AI_CLASSIFIER_MODEL=                     # optional smaller model for routing
PROCAM_AI_KEY=                                  # if the server wants one
PROCAM_AI_TIMEOUT=20
```

Restart, then check: Copilot → the model status shows "available"
(admins see the host; other users only see available / not).

Rules:

- **Private addresses only.** The app refuses known public AI APIs; the
  preflight goes further and FAILs any host that does not resolve to a
  private or loopback address. Do not point this at a cloud AI API, even
  a "private" enterprise plan, without a written business decision.
- A model outage is not a CRM outage: questions still get their
  catalogued answer, without the prose.

### Optional: a self-hosted embedder

Semantic search ("anything about the Kandla job" finding "Kandla port
barge move"). Without it, search is keyword-based and still
permission-filtered.

```bash
PROCAM_AI_EMBED_URL=http://10.x.x.x:11434/v1
PROCAM_AI_EMBED_MODEL=nomic-embed-text
```

Then build vectors: `.venv/bin/python scripts/build_copilot_index.py --embed`.

### The search index

Copilot search reads `copilot_chunk`: each lead's enquiry, notes and
emails, cut into passages stamped with the lead's owner and vertical.
Search filters on those stamps before ranking, so a passage a user may
not see is never scored.

- Reassigning or deleting a lead drops its passages immediately (single
  lead, bulk admin, and both delete paths), so a previous owner stops
  finding its text at once. The search filter is on the owner stamps,
  not the vertical.
- Search narrows by the words and the lead before capping candidates at
  4,000, newest first, so a large index is searched in full.
- New emails and notes are **not** indexed as they arrive. Install the
  nightly rebuild timer (`deploy/procam-crm-copilot-index.*`) or run
  `scripts/build_copilot_index.py` by hand.
- After the production-readiness release, rebuild once: notes were never
  indexed before it.

### Copilot actions (write access)

`PROCAM_AI_ACTIONS=off` by default. When `on`, the Copilot may *propose*
four changes — log an activity, create a reminder, move a stage, reassign
a lead — which only happen after the user presses Confirm within five
minutes. Each is limited to records the user could change anyway, and
logged.

Turn on only with a named business owner's approval, after training.
Turning it off again takes effect at the next restart.

### Audit and analytics

Every question is logged in `copilot_log`: who, the question, which
catalogued answer, the headline (not the rows), the data scope, latency,
and feedback. Admins see it at **/CRM/copilot-analytics**.

`copilot_log` has no retention job. How long questions are kept is a
policy decision (see Production Readiness Report).

---

## Email intake AI

Two separate things:

1. **Extraction** — reading an enquiry email to fill in company, contact,
   cargo and route. Groq first (`GROQ_API_KEY`), Anthropic when Groq's
   answer is weak (`EMAIL_AI_FALLBACK_THRESHOLD`, set `0.0` to never fall
   back). Capped by `AI_DAILY_TOKEN_BUDGET`; machine senders in
   `data/ai_skip_senders.txt` skip AI.
2. **Classifier AI step** (`LEAD_INTAKE_AI=on`) — for emails the rules
   score as uncertain, asks the model which of the ten classes it is.

`LEAD_INTAKE_MODE` controls whether the classifier's verdict decides what
becomes a lead: `enforce` (default), `observe` (record only) or `off`.

Known limitation: email text is passed to the model without
untrusted-content markers. An email written to steer the model can move
an uncertain message into "new lead" with high confidence and skip the
review queue. The rules-only path (`LEAD_INTAKE_AI=off`) is not exposed
to this.

## AI Outreach and business-card OCR

`ANTHROPIC_API_KEY` enables both. Outreach drafts an email from a lead's
details; since the production-readiness release it only accepts a lead
the user can open, and caps free-text instructions at 1,000 characters.
Card OCR sends the card photo.

---

## Decision required: public AI providers

Today, when the keys are set:

- enquiry email bodies go to **Groq** (and sometimes **Anthropic**);
- lead details go to **Anthropic** for outreach drafts;
- business card photos go to **Anthropic**.

Options, for the business owner:

| Option | Effect |
|---|---|
| A. Accept, documented | Keep as is; record the decision, review the providers' data-retention terms, tell staff |
| B. Self-host | Point extraction and classification at the same private model as the Copilot (code change: provider switch) |
| C. Switch off | Remove `GROQ_API_KEY`, `ANTHROPIC_API_KEY`; set `LEAD_INTAKE_AI=off`. Intake falls back to rules; outreach and OCR are unavailable |

Until decided, the preflight keeps a WARN on "public AI providers".
