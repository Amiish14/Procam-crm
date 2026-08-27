# Procam CRM — Commercial Suite Upgrade Plan

**Scope:** extend the existing single-file Flask app (`app.py`, 2,907 lines) plus SPA template (`templates/app.html`) with Quote Management, Rate Intelligence, Competitor Intelligence, and Sales Pipeline enhancement. Existing 9,500+ leads, 30+ APIs, dashboards, KPIs and role-based access remain intact.

**Design principle:** every change is additive. No column is dropped, no endpoint is removed, no lead is retro-touched. Legacy stage names continue to map through `LEGACY_STAGE_MAP`.

---

## Phase 1 — Sales Pipeline: Quoted + Under Negotiation stages

**Deliverable of this session.** Everything below is the roadmap for phases 2–10.

Add two new stages between `RFQ Generated` and `Won`:

```
RFQ Generated → Quoted → Under Negotiation → Won
                       ↘ Won        (alt: direct close)
                       ↘ Lost / On Hold / Not Interested
```

**Touchpoints (all in-file, no schema change):**

| File | What changes |
|------|--------------|
| `app.py:27-30` | Add `Quoted`, `Under Negotiation` to `STAGES_PIPELINE` |
| `app.py:31-38` | Update `STAGE_NEXT` for new progression + allow direct `Quoted → Won` |
| `app.py:40-49` | `LEGACY_STAGE_MAP` — no back-compat entry needed (nothing was called Quoted before), but leave the hook |
| `app.py:1087-1148` | `api_lead_advance`, `api_lead_decision` — validate that the requested stage is in `STAGES_ALL` (already done) |
| `app.py:1102-1130` | `_apply_stage_side_effects` — when a lead reaches `Quoted`, stamp `quoted_date`; when it reaches `Under Negotiation`, stamp `negotiation_start_date` (nullable) |
| `app.py:1616-1620` | Dashboard stage count — add `'quoted'` and `'negotiation'` keys |
| `app.py:1676-1685` | Progression counters — include Quoted / Under Negotiation everywhere Visit Done is in the tuple, so cohort counts don't drop when a lead moves past RFQ Generated |
| `app.py:1739-1762` | Funnel table — add rows |
| `app.py:2732-2740` | Deep-tank count — same tuple update |
| `templates/app.html:488, 506` | Stage `<select>` dropdown — 2 places, add options |
| `templates/app.html:989-991` | Client-side `STAGES` array + `SC` colour map |
| `templates/app.html:1051-1057, 1089` | `PG_TITLES` + route mapping for the new inboxes |
| `templates/app.html:1235-1271` | KPI tile row + funnel colours |
| `templates/app.html:1418-1440` | Team scorecard series |
| `templates/app.html:1516` | Auto-load new inbox |

**Migration:** none required — stages are strings; no DB column to add. Existing rows stay at whatever stage they're on; new stages only flow forward from new user actions.

**Stage-transition matrix delivered in Phase 1:**

| From | To | Allowed |
|------|----|---------|
| RFQ Generated | Quoted | ✓ (normal) |
| RFQ Generated | Lost / On Hold / Not Interested | ✓ (decline before quote) |
| Quoted | Under Negotiation | ✓ (normal) |
| Quoted | Won | ✓ (direct award) |
| Quoted | Lost / On Hold / Not Interested | ✓ |
| Under Negotiation | Won / Lost / On Hold / Not Interested | ✓ |

---

## Phase 2 — Dashboard / KPI / role-view reconciliation

Every count that previously summed cohort membership `stage in (Visit Done, RFQ Generated, Won)` becomes `stage in (Visit Done, RFQ Generated, Quoted, Under Negotiation, Won)`. Ensures totals reconcile across dashboards.

Add new KPIs to the existing KPI Settings page:

- Quotes Issued (count of leads that ever entered Quoted)
- Quote Value (sum of `latest_quote_value` per lead)
- Under Negotiation Value
- Quote-to-Win %
- Avg Quote Turnaround Time (Days between RFQ Generated → Quoted)
- Avg Negotiation Time (Days between Quoted → Under Negotiation and Under Negotiation → Won/Lost)

---

## Phase 3 — Quote Management: upload + tag existing quotations

New tables:

```sql
quotes (
  id, opportunity_id, lead_id, quote_number, quote_date, customer_id,
  currency, quoted_value, revision, validity, status,
  uploaded_by, uploaded_at, remarks, latest_revision_id
)
quote_documents (
  id, quote_id, filename, mime_type, size_bytes, path,
  uploaded_by, uploaded_at, kind  -- 'pdf' | 'email' | 'excel' | 'other'
)
```

Attach to Opportunity via `opportunity_id`, to Lead via `lead_id`. The Opportunity detail drawer gets a new **Quotes** tab that lists all quotes for the opportunity + upload button. Templates on the CRM already have a clean file-upload widget — re-use it.

---

## Phase 4 — Quote revisions

```sql
quote_revisions (
  id, quote_id, revision_no, revised_value, revision_date, reason,
  changed_by, approval_status, document_id, remarks, previous_value
)
```

Number stays as `QT-2026-00123-R0` / `-R1` / `-R2`. Each revision is a new row — the parent quote row's `latest_revision_id` points to the current one. Comparison view is a side-by-side table.

---

## Phase 5 — Service Master + Quote Builder + Terms Library

```sql
services (
  id, code, name, parent_id, active, sort_order
)
quote_lines (
  id, quote_id, service_id, description, quantity, uom,
  rate, currency, taxable_amount, gst_rate, gross
)
terms_clauses (
  id, category, service_ids_json, text, version,
  effective_date, active
)
quote_templates (
  id, name, service_ids_json, header_html, footer_html,
  default_terms_ids_json, active
)
```

Quote Builder is a new wizard: Opportunity → **Create Quote** button → pick services → add lines (auto-computes taxable + gross) → attach terms → save → PDF render via existing render logic.

---

## Phase 6 — Approval workflow + Negotiation

```sql
quote_approvals (
  id, quote_id, revision_id, approver_role, approver_id,
  status, decision_at, remarks
)
approval_rules (
  id, vertical, min_value, max_value, min_margin_pct,
  required_approver_role, sequence
)
negotiations (
  id, opportunity_id, quote_id, revision_id, offered_value,
  customer_target, negotiation_date, remarks, next_followup_at,
  logged_by
)
```

Approval rules resolve on quote submission — if value or margin trips a rule, the quote goes through the configured chain. Recorded per revision so re-submissions after a revision go through again.

---

## Phase 7 — Won / Lost / Awarded Rate + Competitor capture

Extend existing `Competitor` table (`app.py:586`) with `service_id` (multi-service RFQs may quote against different competitors per service).

```sql
rfq_outcomes (
  id, opportunity_id, outcome, closed_date,
  awarded_value, awarded_currency, winning_revision_id,
  customer_po_number, po_document_id,
  lost_reason_id, lost_price_difference, lost_remarks,
  captured_by, captured_at
)
lost_reasons (
  id, name, requires_remarks, active, sort_order
)
competitor_quotes (
  id, opportunity_id, competitor_id, service_id,
  competitor_price, currency, price_basis, price_known,
  source, date_observed, remarks, confidence
)
```

`price_known = false` covers "unknown competitor price" without forcing a value.

---

## Phase 8 — Historical Rate Engine + Similar Quote Search

```sql
rate_history (
  id, opportunity_id, quote_id, revision_id, service_id,
  customer_id, industry, origin_norm, destination_norm,
  distance_km, cargo_type, weight_kg, dimensions_json,
  equipment_id, quote_type,  -- 'quoted' | 'awarded' | 'competitor'
  amount, currency, quoted_at, source_context_json
)
```

Rate rows are written by triggers/hooks — every time a quote is issued, revised, awarded, or a competitor price is entered. `source_context_json` stores whatever fields are relevant for that vertical (heavy transport keeps origin/destination/cargo weight; freight forwarding keeps port codes).

**Similar Quote Search** — a `/api/rates/similar` endpoint that accepts a partial context (customer, service, origin, dest, weight range, distance range, date range) and returns:

```json
{
  "last_quote": {...},
  "avg_quote": 1180000,
  "avg_won": 1075000,
  "competitor_avg": 1095000,
  "comparable_count": 14,
  "won": 5, "lost": 8,
  "win_rate_pct": 38.5,
  "rows": [ {ref_opportunity_id, awarded, quoted, competitor, date, ...} ]
}
```

Never blindly recommend a rate — only show statistics + open-source rows the user can inspect.

---

## Phase 9 — Commercial Dashboard + Competitor Dashboard + Reports

Reuse the existing dashboard framework (`_kpi`, `_metrics` helpers) — add new KPI cards, funnel row, competitor scoreboard. Every card is a filter that drives the underlying data table.

**Reports** — extend existing report endpoints with:

- RFQ Register (already partial — add stage filters)
- Quotation Register
- Quotation Revision History
- Under Negotiation Report
- Won / Lost Register
- Historical Rate Report (with the similar-search filter form)
- Competitor Analysis
- Lost Reason Analysis

Every report respects role-based access and existing filters.

---

## Phase 10 — Historical backfill tools + optimisation

Small admin-only endpoint `/admin/backfill/quotes` accepts a CSV of existing quotes (from Outlook / shared drive) and stores them tagged to Opportunity/Lead. Rate rows are written from the same import so historical statistics fill in over time.

Optimisation pass at the end — indexes on `rate_history(customer_id, service_id, origin_norm, destination_norm)`, `quotes(opportunity_id, revision)`, `competitor_quotes(opportunity_id, competitor_id)`. Server-side pagination on all rate-history queries. Caching on the "similar quote" endpoint for identical filter combinations.

---

## Deployment safety (applies to every phase)

1. Every migration is additive-only. No column dropped. No table renamed. No live data mutated.
2. Every phase runs behind a feature flag in `app.py`. Roll back = flip flag to false, no code revert.
3. Every route added is registered under `/api/quotes/...`, `/api/rates/...`, `/api/competitors/...` — never conflicts with existing routes.
4. Existing endpoints are extended, not replaced. Response shape gains fields, never loses them.
5. Every phase's release is tagged `phase-N-complete`. Rollback command is a documented `git revert phase-N-complete`.

---

## What Phase 1 delivers in this session

Working code that puts **Quoted** and **Under Negotiation** stages in production for the entire pipeline — dropdowns, kanban columns, funnel colours, KPI cohorts, inbox routes, dashboard reconciliation. Nothing broken, every existing lead keeps working. From this point every downstream phase (2–10) adds tables and endpoints; Phase 1 is the foundation everything else hangs off.
