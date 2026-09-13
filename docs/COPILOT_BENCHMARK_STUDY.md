# §13 — Benchmark study: what other CRM assistants do

_v2026-09-13. Written against Procam AI as built, at commit `72c9f5e`._

The brief says to study assistant capabilities in Salesforce, Dynamics,
HubSpot, Zoho, Freshsales, Oracle CX, SAP CRM and Pipedrive, and to
extract the **capabilities worth having** — explicitly not the UI. This
is that extraction, with an honest column for what Procam AI does and
does not do.

A caveat worth stating first: these products' assistant features change
every quarter, and this is a capability study rather than a product
comparison. What matters is the shape of the feature and whether it
earns its place here — not whose version is better.

---

## 1. The capability map

| Capability | Where it is common | Procam AI | Notes |
|---|---|---|---|
| Natural-language record search | All eight | ✅ `universal_search` | Per-entity entitlement; a viewer without Quotes gets the same search without quote rows |
| Conversational analytics ("pipeline by vertical") | Salesforce, Dynamics, Zoho, Oracle | ✅ `pipeline_by_stage` | Dimension is a parameter, not four intents that drift |
| Lead / account summarisation | All eight | ✅ `lead_360`, `account_360` | Every fact is a column read here; the model narrates the assembly |
| Opportunity scoring / win probability | Salesforce (Einstein), Dynamics, Freshsales, Zoho | ❌ **deliberately not** | See §3 |
| Next-best-action | Salesforce, Dynamics, Zoho, Freshsales | ✅ `next_best_action` | Every suggestion states the reason, drawn from the data |
| Activity / engagement suggestions | HubSpot, Freshsales, Pipedrive | ⚠️ partial | Covered by NBA; no separate cadence engine |
| Stale-deal detection | Pipedrive ("rotting"), HubSpot, Zoho | ✅ `stalled_deals`, `leads_stale` | Contact means an activity **or an email** — see §2 |
| Duplicate detection | All eight | ✅ `dq_duplicates` | Presents the existing company-match service; does not re-score |
| Data-quality assistant | Salesforce, Dynamics, Zoho | ✅ `dq_missing_fields`, `dq_lost_no_reason` | Counts by defect, so a gap can be a campaign |
| Relationship intelligence (who knows whom) | Salesforce, Dynamics (via mail/calendar graph) | ❌ not built | Needs calendar and full mailbox access we do not have |
| Email thread summarisation | HubSpot, Dynamics, Freshsales | ✅ `thread_summary` | |
| Document / attachment reading | Dynamics, Oracle, SAP | ✅ `attachment_contents` | Excel, CSV, PDF, text |
| Forecasting | Salesforce, Dynamics, Oracle, SAP | ⚠️ `closing_this_month` only | Honest month window; no predictive forecast — see §3 |
| Conversation intelligence (call recording) | Salesforce, HubSpot, Zoho, Freshsales | ❌ out of scope | No call recording in the CRM |
| Guided selling / playbooks | Salesforce, Dynamics, SAP | ❌ not built | |
| Write actions with confirmation | All eight | ✅ 4 actions, **off** | §10; propose-then-confirm, HMAC-signed |
| Proactive digests / morning brief | HubSpot, Pipedrive, Freshsales | ✅ `morning_brief`, `daily_digest` | |
| Adoption analytics on the assistant itself | Salesforce, Dynamics | ✅ `/copilot-analytics` | Including the unanswered-question backlog |
| Cross-sell / whitespace analysis | Oracle CX, SAP, Salesforce | ✅ `cross_sell_gap` | |
| Per-user permission enforcement in the assistant | All claim it; quality varies | ✅ and tested | §16 suite: 95 tests. See §4 |

**Count: 15 of 20 built, 2 partial, 3 deliberately excluded.**

---

## 2. Three things we do that most of them do not

**Contact means contact.** Every product listed detects stale deals, and
every one we could examine defines staleness on the record's own
`modified` date. In this CRM that would have been catastrophically
wrong: `lead_activities` holds seven rows against eleven thousand leads,
while the email trail holds 1,507. Ranking on the log reported every
open lead as neglected. Procam AI defines contact as an activity **or**
an email, and explicitly excludes a field edit — reporting a typo fix as
customer contact would be a lie the CRM tells itself.

**Answers say when the data cannot support them.** `loss_analysis`
refuses to quote a percentage until twenty losses carry a reason.
`conversion_rate` refuses below twenty decided opportunities. An empty
module says it is empty rather than "everything is quoted". None of the
eight assistants studied advertises a refusal like this, and it is the
single most useful behaviour we built: the failure mode that damages
trust is not a missing answer, it is a confident wrong one.

**Permission travels with the retrieved chunk.** Assistants that do RAG
over CRM text typically filter results after retrieval. Procam AI puts
the owner, secondary, vertical and account on every chunk and filters
the candidate query, so an out-of-scope passage is never scored. It also
drops a lead's chunks when the lead is reassigned — an index holding
yesterday's permissions is the quiet version of the same leak.

---

## 3. Three capabilities we chose not to build

**Opportunity scoring / win probability.** Every major product has it,
and it is the most requested feature in this category. It needs a
labelled history of wins and losses with reasons. This CRM has 168 won
deals with no handover, zero wins recorded in a 30-day window, and fewer
than twenty losses with a reason. A model trained on that would produce
confident scores from noise, and people would act on them. Worth
revisiting once the Lost-Reason capture has run for two quarters.

**Predictive forecasting.** Same reason, plus one of its own:
`expected_close_date` is not maintained — in one vertical, one open
opportunity of 377 had a close date in the current month and roughly 354
had one already in the past. A forecast is arithmetic on top of that
column. Fix the column first.

**Relationship intelligence.** Salesforce and Dynamics build it from the
mail and calendar graph across the whole organisation. We have one
shared mailbox by deliberate design, and reading every employee's
mailbox is a much larger permission conversation than this project.

---

## 4. Where the comparison actually matters

Most of the capability list above is table stakes; any of the eight
would tick most rows. The differences that matter for Procam are:

**Permission model.** Those products enforce their own sharing rules,
which their assistants inherit. Procam AI inherits the Access Matrix —
and Phase 0 existed because the CRM had three different answers to
"what may this user see?". That work was a prerequisite here and is
simply not a question in a hosted product.

**Data residency.** All eight are hosted. §3.1 rules them out for this
purpose regardless of capability, which is why the study is about
capabilities rather than procurement.

**Domain vocabulary.** None of them knows that "ODC ex JNPT on hydraulic
axles" is Project Logistics. Their assistants are configured with
industry packs at best. §7 is where a purpose-built assistant beats a
better-funded general one, and it is a list of words rather than a
model — which is the point.

---

## 5. What to steal next, in order

1. **Cadence / sequence suggestions** (HubSpot, Freshsales). Not "call
   this lead" but "this lead has had one touch in three weeks and its
   stage says Negotiation". A refinement of `next_best_action` that
   needs no new data.
2. **Saved views as questions** (Pipedrive, Zoho). Pinning exists; the
   step beyond is a pinned question that notifies when its answer
   changes — "tell me when a quote over ₹50L goes ten days without a
   reply".
3. **Opportunity scoring**, once the data supports it. Third, not
   first, however much it demos well.

---

_Sources: vendor capability documentation for Salesforce Einstein
Copilot, Microsoft Dynamics 365 Copilot, HubSpot Breeze, Zoho Zia,
Freshsales Freddy, Oracle Fusion CX, SAP Sales Cloud, and Pipedrive AI
Sales Assistant, as described publicly at the time of writing. No
product was purchased or trialled for this study; capabilities are taken
from published descriptions and should be treated as indicative rather
than verified._
