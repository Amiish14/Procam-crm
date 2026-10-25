# Procam CRM — RFQ Chain, Account First, Pre-RFQ Funnel and Lead Journey

**Build prompt · CRM repo `Amiish14/Procam-crm` · 24 Oct 2026**
**Visual reference:** `RFQ_Chain_Tracker_Demo.html` (put it in `docs/reference/`). It shows every screen, field and dropdown below with sample data. Where this prompt and the demo differ, this prompt wins.

---

## 0. Rules for this build

1. **Upgrade the present CRM in place. No impact on existing data.** Migrations only add tables and nullable columns. Never rename, drop or rewrite existing columns. Leads are never deleted.
2. **Build on what exists; do not create a second pipeline or a second master.** The 12 RFQ steps sit under the existing lead stages and RFQ statuses. They run on the existing task engine (`app/models/task_engine.py`, `app/services/task_engine.py`). Accounts stay in `companies`. Audit goes through `app/services/audit.py`. Mail goes through `outbox.enqueue`.
3. **Every release sits behind its own flag**, off by default: `FEATURE_ACCOUNT_FIRST`, `FEATURE_RFQ_CHAIN`, `FEATURE_QUOTE_REV_V2`, `FEATURE_LEAD_JOURNEY`, `FEATURE_PRE_RFQ_FUNNEL`, `FEATURE_RFQ_REPORTS`. With a flag off, the old screen and behaviour must be exactly as today.
4. **Keep the current stack:** Flask, vanilla JS, inline CSS in the existing templates, the existing `crm.css` tokens. No framework and no build step.
5. **Minimum typing.** Every standardised answer is a dropdown from Master Data. Prefill everything the system knows. Users should type only the inputs listed in §9.
6. **Users never see an AI vendor or model name.** AI features are labelled "Procam AI", as already enforced.
7. **Every number and date shown is computed from stored timestamps.** None is typed, except the inputs in §9.

## 1. Stage A — read-only check (report, then STOP)

Before any code, confirm each item with `path:line` evidence and report back:

1. Lead stages in use (`app/services/stage_map.py` JOURNEY, ALIASES, TERMINAL). Also report counts per stage today.
2. RFQ statuses and numbering:
   - `app/models/rfq.py` and `_next_rfq_number` in `app/rfq/routes.py`. Expected format: `RFQ-YYYY-NNNN`, per calendar year.
   - Count of RFQ rows today.
3. Quote numbering and revisions:
   - `_next_quote_number` and `api_revise` in `app/quote/routes.py`.
   - Today each revision takes a new `QUO-YYYY-NNNN`, links only to its parent (`parent_quote_id`), and starts `revision_number` at 1.
   - Count of quotes and of revisions.
4. Task engine: whether `TaskDefinition` / `TaskInstance` can carry the 12 steps with `sla_hours`, `due_at`, `started_at`, `completed_at` and `next_owner_rule` as they are. List any missing column.
5. Account matching and assignment:
   - `account_match.resolve` (7 rules).
   - `auto_assign.decide` / `assign_for`.
   - `OwnerConfirmation`, which is per lead today.
   - `AccountVerticalOwner`.
   - Where the mail intake sets or does not set `lead.company_id`.
6. Pre-sales layer:
   - `presales/models.py`: `ACCOUNT_DEV_STAGES`, `ACTIVITY_KINDS`, `AccountActivity`, `AccountStageHistory`.
   - `presales/services.py`: `change_account_stage`, `log_activity`.
   - Funnel routes in `app/funnel/routes.py`.
7. Quote mail capture to leads@ (`mail_signals.looks_like_quote`, CRM-MAIL-VISIBILITY-01). Also the freight-forwarding thread linking and its unmatched queue (FF-COMMS-01), which reach only freight today.
8. Quote PDF (`quote_pdf`): which sections print today, and whether the sent PDF is stored.
9. Master Data lists: lost reasons, account stages and priorities.
10. Data counts:
    - open leads with no `company_id`;
    - accounts with no owner;
    - accounts without an owner per vertical.

**STOP after the report. Wait for approval.**

## 2. Decisions already taken (do not reopen)

| # | Decision | Date |
|---|---|---|
| D1 | RFQ and quote numbering stay as today: `RFQ-YYYY-NNNN`, `QUO-YYYY-NNNN`, per calendar year, no customer code. | 24 Oct |
| D2 | A revision keeps the first quote's number: `QUO-2026-0045 / Rev-00`, `Rev-01`, `Rev-02`. Every revision links to the first quote. | 24 Oct |
| D3 | RFQ priority names stay: Critical (1–2 days), High (3–4 days), Medium (5–7 days), Low / Auction / Fixed (8–10 days). The turnaround runs from RFQ received to quote sent. | 24 Oct |
| D4 | The RFQ Coordinator registers RFQs: confirms priority and the customer deadline, and ticks the departments involved. | 24 Oct |
| D5 | Only the vertical head or a director can put an RFQ on hold or reactivate it. A reason is always required, and timers pause while held. | 24 Oct |
| D6 | Every new lead has a confirmed account with an owner before anyone works on it. The **vertical head (own vertical) or an admin** clears the Account check queue. | 24 Oct |
| D7 | IOU: the TMS IOU form gets a "Lead no." field (separate TMS prompt). The CRM shows "Open TMS IOU form" with the lead number, and later shows spend read-only. No CRM IOU table. | 24 Oct |
| D8 | RFQ targets are set per month, per vertical, per salesperson. | 24 Oct |
| D9 | The sales funnel sheet lines "Project Cargo Management" and "3PL" map to the Warehousing vertical. | 24 Oct |
| D10 | Margin floors (21 Oct): PTM own hydraulic 38%, market vehicle 14%, mechanical trailer 12%, freight forwarding 10%, warehousing 18%, installation 25%. Head approves up to ₹25 lakh; a director approves above that or below the floor. | 21 Oct |
| D11 | Target is 98% of steps on time. A step "finished early" means within 50% of its standard time; this is a setting. | 24 Oct |
| D12 | The Installation vertical head is **Gowdhaman Rajakrishnan**. The CRM vertical list (CRM-VERTICAL-MASTER-01) shows Sethupathy Sundaram. Change it in Admin → Vertical heads (a setting, not code) before R1, and check the Installation account owners and Account check routing that follow from it. | 24 Oct |

## 2a. Decisions since 24 October

These override the sections they touch. The prompt below is left
as it was written, so the two can be compared.

| # | Decision | Affects | Date |
|---|---|---|---|
| D13 | **Seven lost reasons**, not "the existing 5 + Others": No response · Not interested · Price · Lost to competitor · Customer cancelled or postponed · We could not serve it · Others (free text). Master Data holds exactly these seven and is the source; `app.LOST_REASONS_SEED` is only a fallback. "Not interested" stays separate from "No response" — a customer who says it has answered, and 1,213 leads carry it. 1,336 free-text reasons are left as written, being the only record of why those leads closed. | §8 | 25 Oct |
| D14 | **Pilot is DIR12010 + the Warehousing head (DIR72012)**, by name, not Heavy Transport. Heavy Transport's head would open to 203 queued leads on day one; Warehousing's to 8. Flags now carry a vertical limit as well as a people limit, and the two are an OR. | §11 deploy step 7 | 25 Oct |
| D15 | **A placeholder account counts as no account.** An account named Unknown / New Lead / Info / Email, or one claiming a Procam domain, does not satisfy §3.1 — a lead filed under one *looks* placed, so it never reaches Account check. #983 "Unknown" held 4,435 leads and listed `procamgroup.in`, which would have matched every internal forward on an exact rule. | §3.1 | 25 Oct |
| D16 | **The terms library table is `quote_term_sets`**, not `quote_terms`. | §4.6 | 25 Oct |
| D17 | **Outbound mail is read by its recipient.** Mail from a Procam address to an external one is filed on that customer's open lead; a new lead is created only when they have none, and then with the account on it. Every other rule reads the sender, which is why 167 messages staff sent to customers became orphan leads on 1–3 September. | §3.2 | 25 Oct |

## 3. Release R1 — Account first (`FEATURE_ACCOUNT_FIRST`)

**Goal.** No one works on a lead until it belongs to an account with an owner for that vertical. Mail leads and manual leads both go through the same check.

### 3.1 Gate
- New lead state **"Account to confirm"**: a lead with no `company_id`, or whose account has no owner for the lead's vertical.
- While in this state, these are refused server-side: accept, stage moves beyond New, RFQ registration and quote creation. Use one guard, in the same way as `value_gate.py`.

### 3.2 Mail intake
- Keep `account_match.resolve`. Add `account_match.candidates(...)`, which returns every account hit with the rule that matched (learned contact, contact email, learned domain, account domain, website, GSTIN, company name and similarity %).
- Auto-link only when one of these is true; otherwise the lead goes to Account check:
  - a learned-contact hit;
  - exactly one exact-rule hit (contact, domain, GSTIN) and no name-only alternative.
- A blocked account (client block, live) stays refused as today.
- On link, set `lead.company_id` and assign the owner through `auto_assign.decide` (existing rules).

### 3.3 Account check screen (Accounts page)
- Queue of leads in "Account to confirm". The vertical head sees the own vertical; an admin sees all; others see it read-only.
- Each row shows:
  - the sender, signature, subject and vertical;
  - why it is here: no account / free mail / several matches (likely duplicate) / name only;
  - each candidate account with its matched rule, owner for this vertical and blocked flag. Blocked accounts are disabled.
- **Create a new account** is prefilled from the mail: name from the signature, domain unless it's free mail, and GSTIN from the text.
  - It shows a live duplicate warning while typing (`strict_norm` / similarity ≥ 0.5).
  - It refuses a name similar to a blocked client.
- **Owner for this vertical** is a dropdown, defaulting to the vertical head. It is written to `AccountVerticalOwner` when the account has none.
- **Confirm** links the lead, assigns the owner, calls `account_match.learn` for the sender, and writes an audit row.
- New accounts appear on the existing Thursday new-accounts list. There is no approval step.

### 3.4 Owner confirmation moves to the account
`OwnerConfirmation` today asks per lead. With the flag on, ask once per account × vertical. Leads that follow inherit the answer. Keep the morning digest.

### 3.5 Manual "New lead" (one form, account first)
Fields, in order:
1. **Customer** — type-ahead over accounts; existing accounts are shown first, each with its owner for the chosen vertical and its blocked flag.
2. **Vertical** — dropdown.
3. **How did this lead come** — dropdown of the manual channels only (§6.1).
4. **Requirement** — one line.
5. **Create a new account** — available only after a search, with the same duplicate and blocked checks.

The lead goes to the account owner for that vertical. The creator is recorded and told: "Assigned to X (account owner); you are recorded as creator."

### 3.6 Visibility
- Cockpit tile **Accounts to confirm** for vertical heads (own vertical) and admins.
- The nav badge on Accounts.
- Every lead and RFQ header shows the account, the owner and the rule that chose the owner.

## 4. Release R2 — RFQ chain (`FEATURE_RFQ_CHAIN`)

### 4.1 Steps (task definitions, seeded)

| # | Step | Responsible | Share of window | Closes on (event) |
|---|---|---|---|---|
| 1 | RFQ Received | Sales / BD | 0 (auto) | RFQ mail linked to the lead |
| 2 | RFQ Registration | RFQ Coordinator | 5% | Coordinator confirms priority and deadline |
| 3 | Initial Screening | SCM | 5% | "Information complete" confirmed |
| 4 | Technical Evaluation | Operations / Technical | 15% | Conditions confirmed (tick list) |
| 5 | Site / Route Survey | Operations / Technical | 15% | Conditions confirmed; N/A for air and sea freight, or unticked at registration |
| 6 | Cost Estimation | SCM / Procurement / Finance | 30% | Cost sheet saved (creates the quote draft) |
| 7 | Commercial Approval | Vertical Head / Director | 10% | Quote approved; "Return to costing" reopens step 6 |
| 8 | Quotation Submission | Sales / BD | 20% | Quote sent from the CRM, or quote mail matched (§5.4) |
| 9 | Negotiation | Sales / BD (+SCM) | Procam response time per revision: Critical 4 h, High 8 h, Medium / Low 24 h | Customer accepted, or Lost |
| 10 | Final Bid / Closure | Sales / BD (+SCM) | same | Award expected date recorded |
| 11 | Award / PO | Sales / BD | customer clock (no timer) | PO / LOI recorded → Won |
| 12 | Handover | Sales / BD + Operations | 24 h | Existing Won handover pack sent |

- **Standard time per step** = priority window × share. The shares are settings in Master Data.
- **People.** Who is responsible for each step comes from:
  - the account owner (sales);
  - the RFQ Coordinator role;
  - the vertical's SCM and Ops defaults, kept in a small Admin table per vertical (not hard-coded);
  - the vertical head.

  "Pass to" is prefilled; change it only when needed.
- Steps open and close only from events. A manual close is allowed only for steps 3–5 ("Confirm"), and is always audited.

### 4.2 Timing
- **Colours:** red = overdue, orange = due within min(24 h, 50% of the step time), green = otherwise, purple = on hold, blue = customer clock. Always show text with the colour ("Overdue 6h", "Due in 3h"). This reuses the existing timer rule, extended for short Critical steps.
- **Result stored on close:** Early (≤ 50% of standard), On time, or Delayed.
- **Hold:** time on hold is stored per step (`paused_seconds`) and never counted.

### 4.3 Reasons (only on exceptions)
- **Closing a Delayed step** needs a **delay reason** dropdown. The value is pre-selected from evidence, for example:
  - a vendor rate request still open → Awaiting vendor rate;
  - a clarification mail open → Customer clarification pending.
- **Closing an Early step** needs a **good-practice reason** dropdown.
- "Others" needs remarks.
- Lists: §8.

### 4.4 Registration (step 2, RFQ Coordinator)
One panel:
- **Priority** dropdown, suggested from the customer deadline (< 2 days Critical, < 4 High, < 7 Medium, else Low).
- **Customer deadline**, prefilled from the RFQ mail by the existing mail-reading feature.
- **Tick boxes:** Technical evaluation needed, Site / route survey needed.

The RFQ number is generated at registration (`RFQ-YYYY-NNNN`, as today). If the customer deadline falls before the end of the priority window, show a warning.

### 4.5 Screening (step 3)
- **Dropdown:** "Complete — nothing missing" / "Missing: drawings / dimensions" / "Missing: site address / route" / "Missing: scope of work" / "Missing: required date".
- **"Missing …"** sends the customer clarification mail from the CRM and marks the step "waiting for customer". The step stays open; "Complete" closes it.

### 4.6 Department conditions (steps 4, 5, 6, 7)
- A tick list proposed from the existing terms library (`quote_terms`) for the vertical.
- Plus one optional free line, with a type dropdown (§8) and a visibility dropdown (Customer-facing / Internal note).
- New table `rfq_conditions`: `rfq_id`, `step_no`, `by`, `type`, `text`, `visibility`, `created_at`, `approved`.

### 4.7 Hold and priority (D5)
- Buttons for the vertical head (own vertical) and directors only.
- **Hold:** reason dropdown (§8) plus remarks. Stores held by, time, stage and remarks. Reactivating keeps the full history and adds the held time to `paused_seconds`.
- **Priority change:** dropdown plus remarks. Every responsible person on the RFQ is mailed through the outbox, and the change is audited.

### 4.8 Screens
- **My Work** (existing): "Waiting on me" lists both pre-RFQ next actions and RFQ steps, soonest first, each with the timer and an **Act** button that opens the lead record on the next action.
- **RFQs page → Chain board:**
  - a "Where RFQs are sitting now" bar per step, split by colour; clicking a bar filters the list;
  - a list with the 20-box journey strip, the person holding the RFQ now and the timer.

  Visible to everyone in the chain. Sales see their own and chain RFQs; heads see their vertical; admins see all.

## 5. Release R3 — Quote additions (`FEATURE_QUOTE_REV_V2`)

### 5.1 Revision numbering (D2)
- New columns:
  - `quotes.root_quote_id` — the first quote; equal to the row's own id for Rev-00;
  - `quotes.rev_no` — starts at 0.
- **Display number:** `<root.quote_number> / Rev-NN`.
- `api_revise` keeps cloning and keeps `parent_quote_id`. It sets `root_quote_id` and `rev_no = max + 1`, and reuses the root's number for display, mail subject and PDF file name. The internal `quote_number` of the new row is not shown to users.
- **Backfill** existing chains by walking `parent_quote_id`: preview counts first, then apply, with an audit row each.
- A revision needs:
  - a reason, chosen from a dropdown;
  - approval under the D10 rules;
  - sending.

  On send, the previous revision becomes Superseded.

### 5.2 Consolidation for the quote
- At cost-sheet save, approved **customer-facing** `rfq_conditions` flow into `QuoteSpecialCondition` and the quote's exclusions. Internal notes never flow.
- Duplicates (same normalised text) are merged.
- Conflicts block approval with the message "Conflict to resolve before approval". A conflict is the same topic (validity, payment, free time / detention) with different numbers.

### 5.3 Cost sheet and submission gate (step 6 → 8)
- **Step 6 panel:**
  - Currency dropdown.
  - Cost, prefilled from rate sourcing (the lowest received vendor reply).
  - Quoted price.
  - Live margin against the D10 floor; below the floor shows "needs director".
- **Submit refuses** an empty amount or currency. This uses the existing value gate.

### 5.4 Quote mail matching for all verticals
- Quote mail captured at leads@ (existing) is matched to a lead using these rules, in order: quote number + revision, then RFQ number, then the thread headers.
- Only a verified match closes step 8. It records: sent time, recipient, sent by, revision, amount and mail reference.
- Anything else goes to the **Unmatched quote mail** queue (RFQs page; RFQ Coordinator, heads and admins). Each row has two actions:
  - "Link to lead…", where a suggestion from the same customer domain is preselected;
  - "Not a quotation".
- Reuse the FF-COMMS-01 linking order and unmatched queue for all verticals; do not write a second one.

### 5.5 PDF (customer copy)
- Add:
  - the Business Unit letterhead (logo, name, address and GSTIN from the BU master);
  - a customer block;
  - "Our ref RFQ-…, Lead #…";
  - a confidentiality clause;
  - "For <BU> — Authorised Signatory".
- Remove the internal status line from the customer copy.
- Store the exact PDF sent against its revision.

## 6. Release R4 — Pre-RFQ funnel and lead journey (`FEATURE_PRE_RFQ_FUNNEL`, `FEATURE_LEAD_JOURNEY`)

### 6.1 New lead fields (all on `leads`, nullable)

| Field | Values | Set by |
|---|---|---|
| `lead_channel` | a–l from the sales funnel sheet (§8) | **Automatic** for 4 channels:<br>• mail from outside to leads@ → Inbound Enquiries<br>• forwarded by a director (`forwarded_by_email` in the director list) → Management Leads<br>• account has an earlier Won → Existing Customer / Repeat<br>• auction / tender lead type → Tender / E-Procurement<br>Otherwise a dropdown on New lead. |
| `expected_rfq_timing` | Immediate / Short / Medium / Long / Nurturing / Dormant | Dropdown, once, when "Meeting held" is logged |
| `capability_fit` | Yes — standard / Yes — needs site visit / Partly — with partner / No | Dropdown, once. "No" closes the lead as Lost "Technical / capability not matched". |
| `rfq_expected_date` | date | Once, when "RFQ expected" is logged; prefilled from project intelligence if present |

### 6.2 Activity moves the stage (no separate stage change)
**Log activity** is one form on the lead record:
- **Kind** dropdown: Phone call, Email, LinkedIn message, Meeting planned, Meeting held, Requirement received, Capability check, RFQ expected.
- Date, prefilled with today.
- **With** — a dropdown of the account's contacts.
- One conditional field per kind.
- An optional note.

Saving writes an `AccountActivity` row and moves `Company.dev_stage` (through `change_account_stage`, with history) to the matching stage:

| Activity | Moves to |
|---|---|
| call / email / LinkedIn | Contact Established |
| meeting planned | Meeting Planned |
| meeting held | Meeting Completed |
| requirement | Opportunity Identified |
| capability Yes / Partly | (capability matched; this is a checkpoint, not a stage) |
| RFQ expected | RFQ Expected |

The RFQ mail arriving moves it to Active Account and opens step 1. Stages only move forward from activity; moving back needs an admin with a reason.

### 6.3 Targets (D8, D9)
- New table `rfq_targets`: `month`, `vertical`, `emp_code`, `target_rfqs`. Admin edits it on `/admin/crm-rules`.
- Channel standard conversions (the sheet's %) are a Master Data list.
- The **Funnels** page adds:
  - "RFQ targets — month": salesperson × vertical, with target, leads needed (target ÷ that person's channel-mix conversion), RFQs received this month, % achieved and RFQ-expected forecast;
  - "Lead channels — plan vs actual": standard conversion, planned leads, expected RFQs not rounded per row, actual leads and RFQs, and conversion;
  - "Leads reaching each stage", with stage-to-stage % (inbound / repeat / tender leads skip the early stages);
  - "Expected RFQ timing" counts shown as labels, never as timer colours;
  - "RFQ expected — forecast".
- Every bar and row opens the filtered Leads list.

### 6.4 Lead record (one hub; every row anywhere opens it)
- **Header:**
  - lead no. and RFQ no.;
  - priority and status chips;
  - account, owner and owner rule;
  - vertical and channel (with "automatic" where set by the system);
  - requirement and customer deadline;
  - spend (later, from TMS).
- **Next action** panel at the top. It shows the form for the current step (dropdown-first) or "Waiting on X" for viewers who cannot act. Head and director controls (priority, hold) sit in the same row.
- **Journey flowchart:**
  - lane 1 "Before the RFQ" with 8 boxes; lane 2 "RFQ to order" with 12 boxes, then Won or Lost;
  - each box shows done (✓, date, who and time taken), skipped (dashed), current (coloured by timer) or future (person responsible);
  - clicking a box shows its detail: who, when, time against standard, result and reason, and activities.
- **Time-per-step chart:** a horizontal bar per step from its start to its close or now. Bars are coloured by result, with the standard-time mark; clicking a bar shows the step.
- **Tabs:** Journey · Steps & time · Activity · Conditions · Quotes (with PDF preview) · Spend (IOU, read-only) · Audit.
- The **"Open TMS IOU form"** button deep-links to the TMS IOU create page with `crm_lead_no=<no>` (see the TMS prompt). Until the CRM–TMS link exists, the Spend tab shows "IOUs for this lead are in the TMS".

### 6.5 Cockpit (existing page, panels added)
- **Tiles:** Waiting on me · Overdue · Accounts to confirm (heads / admins) · Unmatched quote mail (coordinator / heads / admins) · RFQs expected ≤ 30 days.
- **"Where my work is":** one journey strip per visible lead, soonest due first.
- Search also finds RFQ numbers and quote numbers with revision.

## 7. Release R5 — Reports (`FEATURE_RFQ_REPORTS`; heads, coordinator, admins)

**Tiles** — each opens its list:
- RFQs received, Active, On hold, Quoted, Won, Lost, Overdue, Due today;
- Avg response (received → quote sent, held time excluded);
- Prospective revenue (open quotes, latest revision, converted to INR at the existing FX settings);
- Won revenue and Conversion (won ÷ closed).

**Panels** — clicking a bar, row or cell opens the RFQ list filtered by step or person:
- Time per step against standard, with the slowest step called out.
- Workload & completion. This is the layout of the SCM "200+ RFQs Analysis" sheet: person, RFQs by priority, on-time %, early count, "Std. 98%" Achieved / Below target, gap, open / late.
- Person × step heatmap (average time ÷ standard).
- Delay & good-practice reasons by reason, function, employee, customer, vertical and month.
- Sales spend by lead, vertical and channel. This shows "available after the CRM–TMS link" until then.

The weekly Thursday report adds the bottleneck step, the workload table and the top delay reasons.

## 8. Master Data lists to seed (editable, audited)

- **Delay reasons:** Awaiting vendor rate · Shipping line response pending · Insufficient RFQ information · Technical clarification pending · Route survey required · Customer clarification pending · Internal approval pending · Workload / resource constraint · System issue · Others (remarks required).
- **Good-practice reasons:** Existing vendor relationship · Historical rate / database used · Complete customer information · Early vendor engagement · Standard solution available · Quick interdepartmental coordination · Others.
- **Hold reasons:** Requirement closed by customer · Cancelled by customer · Postponed by customer · No longer commercially relevant · Others.
- **Lost reasons:** keep the existing 5 + Others.
- **Condition types:** Condition · Assumption · Limitation · Exclusion · Special requirement · Customer-facing term.
- **Lead channels with standard conversion:**

  | Channel | Conversion |
  |---|---|
  | a Management Leads | 50% |
  | b Existing Customer / Repeat | 60% |
  | c Customer Referrals | 30% |
  | d Customer Visits | 35% |
  | e Networking | 10% |
  | f LinkedIn | 10% |
  | g Email Outreach | 5% |
  | h Cold Calling | 5% |
  | i Inbound Enquiries | 50% |
  | j Industry Events & Exhibitions | 15% |
  | k Channel Partners | 20% |
  | l Tender / E-Procurement Portals | 10% |

- **Expected RFQ timing:** Immediate (≤ 1 month) · Short-term (1–3) · Medium-term (3–6) · Long-term (> 6) · Nurturing · Dormant.
- **Screening outcomes** (§4.5), **capability fit** (§6.1), **revision reasons:** Customer asked for a revised price · Scope changed · Quantity changed.
- **Step shares and response hours** (§4.1), **early threshold** 50%, **on-time target** 98%.

## 9. Everything a user types

| Input | Where | Form |
|---|---|---|
| Lead channel | New lead (manual channels only) | Dropdown |
| Activity kind (+ one conditional field) | Lead record | Dropdown + date |
| Expected RFQ timing | "Meeting held" | Dropdown |
| Capability fit | "Capability check" | Dropdown |
| RFQ expected date | "RFQ expected" | Date |
| Priority + deadline | Registration | Dropdown (suggested) + prefilled date |
| Screening outcome | Step 3 | Dropdown |
| Conditions | Steps 4–7 | Tick list + optional one line |
| Cost / quoted price | Step 6 | Prefilled numbers |
| Approval decision | Step 7 | Dropdown |
| Customer response / revision | Step 9 | Dropdown + amount |
| PO no. | Step 11 | Text |
| Delay / good-practice reason | Only on late / early close | Dropdown (pre-selected) |

Everything else comes from mail, the account, rate sourcing, the quote builder or timestamps.

## 10. Acceptance tests (staging, before any flag goes on in production)

1. With all flags off, every existing test passes and the existing screens are unchanged.
2. A mail lead from a known domain with one owner links by itself. A free-mail lead goes to Account check. A blocked sender creates no lead.
3. A lead in "Account to confirm" cannot be accepted, registered or quoted (server refuses all four paths).
4. A sales user cannot confirm an account. The vertical head (own vertical) and an admin can.
5. Confirming an account teaches the sender; the next mail from that sender links by itself.
6. Each step closes only on its event: saving the cost sheet closes step 6, approval closes step 7, sending closes step 8.
7. Closing a late step without a delay reason is refused. "Others" without remarks is refused.
8. Hold pauses the timer. Reactivation adds the held time to `paused_seconds`. Only a head or director sees Hold.
9. A revision of `QUO-2026-0045` displays as `QUO-2026-0045 / Rev-01`, links to the root, and needs a reason and approval. Sending it supersedes Rev-00.
10. Internal conditions never appear on the PDF or in the quote mail. A validity conflict blocks approval.
11. Quote mail with the quote number and revision closes step 8 and records recipient, revision, amount and reference. Mail without a number goes to the unmatched queue.
12. Logging "Meeting held" sets the timing and moves `dev_stage` to Meeting Completed, with a history row.
13. Funnel and report tiles equal the lists they open: same filters, same counts.
14. Targets table: RFQs received this month per salesperson × vertical match the RFQ list for that month.
15. No user-visible text names an AI vendor or model.

## 11. Delivery

- Deliver one PR per release (R1 → R5), in that order, each behind its flag.
- Each PR includes:
  - migrations (additive);
  - a backfill script with `--preview` / `--apply` (revision roots, `lead_channel` for past leads where the automatic rules apply);
  - tests and a release note listing settings to enter on `/admin/crm-rules`.
- **Deploy:**
  1. backup and restore test;
  2. `git pull`;
  3. migrations;
  4. backfill preview → send counts → apply after OK;
  5. restart;
  6. login check;
  7. turn the flag on for DIR12010 only, then for the pilot vertical (Heavy Transport), then for all.
- **Rollback** = flag off. No data is removed.

**Not in this build:** the CRM–TMS link (IOU spend read-back, Won → TMS project), the vendor master migration (waiting for Sumit's list), and key-account margin overrides.
