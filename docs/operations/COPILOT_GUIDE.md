# Procam AI Copilot Guide

For administrators and the people who support the Copilot: what it can
answer, how it decides, and why it cannot show anyone more than the CRM
already lets them see. Configuration (model host, embedder, index
timer, actions switch) is in the [AI Configuration Guide](AI_CONFIGURATION_GUIDE.md).

---

## How a question is answered

```
question ─► scope ─► record context ─► intent ─► entitlement ─► approved query ─► narration ─► audit
            (who)    (the page, if       (rules,   (Access       (scope injected)  (optional   (copilot_log)
                      visible)            memory,   Matrix)                         model)
                                          model)
```

1. **Scope** is resolved from the signed-in session through the Access
   Matrix (`app.access.scope`). Nothing in a request can change it.
2. **Record context** — the lead, opportunity or account the panel is
   open on — is checked against that scope before anything uses it.
3. **Intent**: the question is matched to one of 65 catalogued intents.
   Rules first; then the conversation's previous turn; then, only if a
   private model is configured, the model picks an intent and its
   parameters. The model never writes a query.
4. **Entitlement**: the intent's permission (for example `module.quotes`)
   is checked. Without it the answer says so and names who can grant it.
5. **The approved query** for that intent runs with the scope injected,
   through `app.access.scope` / `app.access.records`.
6. **Narration**: with a model, a short paragraph written from the result
   only; without one, the result's own headline.
7. **Audit**: one `copilot_log` row per question — never the answer rows.

The Copilot is read-only. Write actions stay behind `PROCAM_AI_ACTIONS`
and a propose → confirm step (see the AI Configuration Guide).

---

## Security model

| Guarantee | How it holds |
|---|---|
| An answer never includes a record the asker cannot see | Every intent receives an already-resolved scope and reaches data only through the scope/records helpers. `tests/test_copilot_rbac.py` sweeps every intent; `tests/test_copilot_insights.py` probes each new intent with another owner's account name, id, page context, lead and opportunity. |
| A named out-of-scope account gives routing only | "Is X already a customer", "account health for X" and similar return that X exists and who owns it, marked restricted, and nothing else (§5.1 / §6.6). |
| Page context cannot widen anything | The panel sends `{type, id}`. The server checks it with the same helpers (`records.company_access` for accounts, `may_view_quote` / `may_view_rfq` for documents). A record the viewer may not see is answered as restricted and its name is never echoed. |
| Conversation memory never crosses users | The conversation id is signed with `SECRET_KEY` for one user, and memory reads only that user's own `copilot_log` rows. Scope is re-resolved on every turn, so a revoked permission bites on the next follow-up. |
| The model cannot run a query | It returns an intent key and parameters, which go through the same entitlement and scoped handler as a rule match. Public AI hosts are refused in code. |
| Answers never expose internals | `how_answered` is built from the intent label and parameter values in words. No SQL, host name or model reasoning is stored or returned. |
| Search text is permission-filtered before ranking | Chunks carry their lead's owners; the scope filter is on the candidate query, and every optional filter is ANDed onto it. |

---

## The intent catalogue

65 intents. `—` means no module permission is needed; the answer is
still confined to the asker's scope.

| Area | Intent | Permission | Asked as |
|---|---|---|---|
| My day | `my_day` | — | what needs my attention today |
| | `followups_due` | — | which follow-ups are due |
| | `my_tasks` | — | my open tasks |
| | `next_best_action` | — | who should I call this week · what's next here |
| Leads | `leads_open` | — | my open leads |
| | `leads_new` | — | new leads this week |
| | `leads_stale` | — | stale leads |
| | `leads_no_next_action` | — | leads with no follow-up date |
| | `leads_by_stage` | — | leads by stage |
| | `lead_sources` | — | where do our leads come from |
| | `leads_unassigned` | — (company-wide scope only) | unassigned leads |
| | `lead_360` | — | summarise this lead |
| RFQs | `rfqs_unquoted` | module.rfq | which RFQs have I not quoted · overdue RFQs |
| | `rfqs_recent` | module.rfq | RFQs received this week |
| | `rfqs_by_status` | module.rfq | RFQs by status |
| | `rfqs_high_value_recent` | module.rfq | new RFQs above 50 lakh this week |
| Quotes | `quotes_pending_approval` | module.quotes | quotes pending approval |
| | `quotes_expiring` | module.quotes | quotes expiring this week |
| | `quotes_awaiting_reply` | module.quotes | quotes waiting for a reply |
| | `quotes_recent` | module.quotes | quotes sent this week |
| | `quotes_by_status` | module.quotes | quotes by status |
| | `quotes_above` | module.quotes | quotes above 50 lakh |
| | `last_quote_for_account` | module.quotes | what did we last quote Tata Steel |
| | `quote_turnaround` | module.quotes | how long do we take to quote |
| Pipeline | `pipeline_value` | module.funnels | what's my pipeline worth |
| | `pipeline_by_stage` | module.funnels | pipeline by vertical |
| | `pipeline_health` | module.funnels | pipeline health |
| | `top_opportunities` | module.funnels | biggest deals |
| | `stalled_deals` | module.funnels | stalled opportunities |
| | `opportunity_risk` | module.funnels | which deals are at risk |
| | `opps_overdue_close` | module.funnels | opportunities past their close date |
| | `closing_this_month` | module.funnels | closing this month |
| | `account_pipeline` | module.funnels | open deals for Tata Steel |
| Accounts and Company 360 | `account_status` | — (detail needs entitlement) | is Tata Steel already a customer |
| | `account_owner` | — | who handles JSW |
| | `account_health` | — (own accounts; routing only otherwise) | account health for Tata Steel · customer health |
| | `account_360` | reports.accounts | summarise Godrej |
| | `cross_sell_account` | — (routing only outside scope) | what else can we sell to JSW |
| | `cross_sell_gap` | reports.accounts | cross sell opportunities |
| | `relationship_map` | — (routing only outside scope) | who knows Tata Steel |
| | `key_contacts` | — (routing only outside scope) | who is the contact at Godrej |
| | `top_accounts` | reports.accounts | our biggest customers |
| | `accounts_inactive` | reports.accounts | which customers have gone quiet |
| Handovers and projects | `handovers_awaiting_po` | module.handovers | won deals awaiting PO · pending handovers |
| | `handover_missing_po` | module.handovers | won deals with no PO |
| | `handovers_recent` | module.handovers | what was handed over this month |
| Wins, losses, performance | `deals_won_recent` | — | what did we win this month |
| | `deals_lost_recent` | — (reasons need reports.competitor) | what did we lose this month |
| | `loss_analysis` | reports.competitor | where are we losing |
| | `conversion_rate` | reports.accounts | win rate |
| | `win_rate_by_vertical` | reports.accounts | win rate by vertical |
| | `my_win_rate` | — (asker's own records only) | my win rate |
| | `my_performance` | — | how am I doing |
| | `team_workload` | reports.action | team workload |
| | `team_activity_gap` | reports.action | who hasn't updated CRM this week |
| | `daily_digest` | reports.action | what happened in sales today |
| Data quality and admin | `dq_missing_fields` | admin.master | data quality |
| | `dq_duplicates` | admin.master | duplicate accounts |
| | `dq_lost_no_reason` | admin.master | lost leads with no reason |
| | `dq_accounts_no_owner` | — (ownerless accounts linked to the asker's work; all of them for a company-wide scope) | accounts with no owner |
| | `intake_review_pending` | admin.master (company-wide scope; counts only) | intake review queue |
| Email, documents, search | `thread_summary` | — | what did the customer ask in the latest email |
| | `attachment_contents` | — | what does the BOQ say |
| | `search_text` | — | anything about the Kandla job |
| | `universal_search` | — | find Siemens |

Most list intents also take a **service filter** ("only Project
Freight"). A filter matches every spelling of the service — Project
Freight / Project Logistics / PFM, Heavy Transport / Transportation,
Customs Clearance / Customs — because leads, accounts and people spell
them differently.

### The question library

`app/copilot/library.py` holds 337 real phrasings that must route on the
rules alone, 24 out-of-scope questions that must stay unrecognised
(invoices, vehicle tracking, forecasts), and 8 phrasings marked
`MODEL_ONLY` that the rules deliberately leave to a model. The library
test fails the build if a phrasing stops routing, if the rules send a
model-only phrasing to a different intent, or if a phrasing is listed
twice.

**Adding a question the Copilot misses** (the "unmatched" column on
/CRM/copilot-analytics is the backlog): add the phrasing to `QUESTIONS`
with its intent, run `tests/test_copilot_library.py`, and adjust or add a
rule in `service._PATTERNS` until it routes. Specific rules go before
general ones.

**Adding an intent**: register it with `@intent(...)` in `queries.py` or
`insights.py`, take `(scope, params)`, read only through the scope and
records helpers, return a `Result`, add library phrasings, follow-up
chips in `service.FOLLOW_UP_CHIPS`, and a leak test with an owner and an
admin control.

---

## Health and risk scores

Every score is a sum of named factors with fixed points. The numbers
are constants in `app/copilot/insights.py` / `queries.py`, and each
answer restates them in its notes so the reader can recompute it.

**Account health** (100 points; counted only over records the asker can see)

| Factor | Points |
|---|---|
| Recent contact — within 30 days / within 90 days | 30 / 15 |
| At least one open opportunity | 20 |
| Won business — in the last 365 days / ever | 20 / 10 |
| A follow-up or next action dated today or later | 15 |
| A primary owner is set | 10 |
| At least one contact on record | 5 |

70+ healthy · 40–69 watch · below 40 at risk. "Customer health" is the
same score over accounts with won business.

**Pipeline health** starts at 100 and loses 20 for each check at risk,
10 for each to watch. 80+ healthy, 60–79 watch.

| Check (share of open opportunities) | Watch above | Risk above |
|---|---|---|
| No expected close date | 10% | 30% |
| Close date already passed | 10% | 25% |
| Unchanged for 30+ days | 20% | 40% |
| No owner | 0% | 5% |
| Largest deal's share of open value | 25% | 50% |

**Opportunity risk** (additive): close date passed 30 · no close date 15
· unchanged 30+ days 25 (14–29 days 15) · probability 25% or less 10 · no
owner 10 · ₹1 crore or more at stake 10. 50+ high, 25–49 medium.

**Next best action**: score = the strongest reason (customer waiting on a
reply 50 · follow-up overdue 40 · negotiation idle 5+ days 35 · quoted
and idle 3+ days 30 · RFQ not yet quoted 25 · no contact 14+ days 15 ·
never contacted 10) + 1 point per ₹10 lakh of value (max 30) + 1 point
per 3 idle days (max 20). "Contact" is an activity or an email, never an
edit. On a lead's page, "what's next here" lists every applicable step
for that lead, a quote nearing the end of its validity, and the next
stage.

---

## Search ranking (RAG)

`search_text` and the retrieval layer (`app/copilot/retrieval.py`):

1. **Permission first.** Candidates are chunks whose owner or secondary
   owner is in the asker's scope. Optional filters narrow inside that:
   date range, owner, source type (note, email, enquiry), account, lead.
   The intent reads "in the notes", "last 30 days" and "since
   2026-08-01" out of the question.
2. **Words.** TF-IDF over the candidates, plus the desk's vocabulary:
   ODC, OOG, CHA, B/L, ETA/ETD, HS code, FCL/LCL, RoRo, breakbulk,
   project cargo, heavy lift, reefer, demurrage/detention, EXW, FOB, CIF,
   DAP, DDP, LOLO, AWB, BOQ each find their spelled-out forms and back.
   Short forms are matched on word boundaries ("bl" never matches
   "table"); an expanded spelling counts 0.6 of a typed word.
3. **Phrase and nearness.** ×1.5 when the words appear together in
   order; up to ×1.3 as they cluster.
4. **Kind of text.** Enquiry ×1.25, note ×1.15, inbound email ×1.0,
   outbound email ×0.9; a passage that is mostly a signature block ×0.5.
5. **Recency**, with a **180-day half-life**, moving a score by at most
   35% — relevance still decides.
6. **Hybrid** when a private embedder is configured: 0.65 × cosine +
   0.35 × normalised lexical score, then the same kind and recency
   weights. Without one, lexical only.

Every hit carries a citation — lead, source type and the date the
passage was written.

**After deploying this release, rebuild the index once**
(`.venv/bin/python scripts/build_copilot_index.py`). Passages indexed
before it carry the lead's last-update date rather than the date each
email or note was written, which weakens recency ranking and the dates
on citations until they are rebuilt.

---

## Conversation memory and follow-ups

- The first answer returns a `conversation_id`; the panel sends it back
  with each question and forgets it when the user presses **New**.
- Memory is the same user's last **6** questions in that conversation,
  from the last **30 minutes**, read from `copilot_log`. No new table or
  column.
- Follow-ups resolved without a model: a window ("what about last
  month", "past 2 weeks", "today"), a service ("only Project Freight"),
  an amount ("over 1 crore"), "overdue", "weighted", "by owner", and an
  account ("and for Tata Steel?" — switching to the account version of
  the question where there is one, or offering options where there is
  not). A modifier the previous question cannot take is reported in the
  answer, not silently dropped.
- **`SECRET_KEY` must be set in `.env` and the same for every worker.**
  If it is missing, each gunicorn worker generates its own, and a
  follow-up that lands on the other worker quietly starts a new
  conversation.

---

## Record context

When the panel is opened on a record, questions about "this" record —
"summarise this", "what's next here", "who is the contact", "who knows
this account", "cross-sell here", "what's the risk here" — are answered
for it.

The panel finds the record from, in order:

1. `data-copilot-type` and `data-copilot-id` (and optional
   `data-copilot-label`) on any element of the page;
2. `window.ProcamAI.setContext({type, id, label})`, for pages that open a
   record without changing the URL (pass `null` to clear);
3. the URL: `/leads/<id>`, `/companies/<id>`, `/quotes/<id>`, `/rfqs/<id>`,
   or `?lead=`, `?opp=`, `?account=` on the main app.

A quote or RFQ is answered through its lead, or its account. The
context chip can be dismissed; it says when the record is outside the
viewer's access, and the answer is then restricted.

**Known gap:** the main app opens a lead in a detail view without
changing the URL, so a lead opened by clicking in a list is not detected
until `templates/app.html` calls `window.ProcamAI.setContext` when it
opens (and `null` when it closes) or sets the data attributes on the
detail view. Deep links (`/leads/<id>`, `?lead=`) are detected today.

---

## Confidence, "how I answered", citations

| Field | Meaning |
|---|---|
| `confidence` | `high` — a rule matched the wording, or the page supplied the record; `medium` — matched through a synonym, or a follow-up to the previous question; `low` — the model chose the intent; empty for clarifications and restricted answers |
| `how_answered` | "Answered with “Stale leads” · last 14 day(s) · service: Project Freight · matched your wording · your own records" |
| `citations` | one per record the rows link to; for search hits also the source type and date |
| `follow_ups` | 2–3 next questions, only ones the asker is entitled to |
| `clarification` | `{question, options: [{label, question}]}` — 2–4 options, each a complete question |
| `context`, `resolution`, `conversation_id` | the record in context (label only if visible), how the intent was reached, and the conversation to continue |

All existing response fields are unchanged; these are additions.

## Clarification

Instead of guessing, the Copilot asks when:

- the question is too broad — "quotes", "leads", "health", "risk",
  "status" — offering the specific questions the asker may use;
- an account name matches several accounts in the asker's scope and none
  exactly — listing them;
- a needed value is missing — "quotes above …" offers thresholds;
  "who knows", "key contacts", "cross sell" or "open deals" with no
  account and no page context offer the asker's most recently active
  accounts. ("Account health" with no account is a ranked list of the
  asker's accounts, weakest first.)

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Follow-ups ("what about last month") are not understood | No `SECRET_KEY` in `.env` (see memory above), more than 30 minutes since the previous question, or the panel was reset with New |
| "summarise this" asks which lead | The page does not expose its record — see Record context, known gap |
| Search results show old dates | The index predates this release; rebuild it |
| An account question returns only the owner's name | The account is outside the asker's scope — working as designed |
| A new phrasing goes unanswered | Add it to the library and a rule (see The question library) |
