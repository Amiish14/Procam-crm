# RFQ-DEMO-PARITY — gap list

What `docs/reference/RFQ_Chain_Tracker_Demo.html` shows, against
what the live screens showed, screen by screen and panel by panel.

Every row was found by photographing the two side by side at
1440 px and 390 px. The pairs are in this folder:

    <screen>--demo-desktop.png      <screen>--demo-phone.png
    <screen>--live-desktop-before.png   …-phone-before.png
    <screen>--live-desktop-after.png    …-phone-after.png

The live half runs on a throwaway database seeded by
`scripts/parity_world.py`; the real one is never opened. Re-take
them with:

    .venv/bin/python scripts/parity_shots.py --stage after

**Verdict** is what the first pass found. **Now** is the state of
the branch. Every row reads `same`, or says why it cannot.

---

## 1. Account check — `/accounts/check`

| Panel | Verdict | What differed | Now |
|---|---|---|---|
| Page shell | differs | No top bar; a page-local `.ac-*` stylesheet of its own | same — `_crm_topbar.html` and the shared `.rfq-*` components |
| Heading | differs | "Account check" as the page title | same — "Accounts", with the demo's lede |
| Pending item | differs | A flat card; rules and owner as grey chips | same — the demo's `action` block, radio-style candidate rows, the reason as a RAG word |
| Suggested match | same | — | same |
| Candidate owner | differs | Showed `PAR_SALES`, an employee code | same — the name; the code stays in the payload as `owner` for callers that match on it |
| Create-account form | differs | Bare inputs in a row | same — the demo's labelled field grid |
| Bulk confirm | same | — | same |
| "Only the vertical head can confirm" | same | — | same |

## 2. New accounts this week — `/accounts/check#owners`

| Panel | Verdict | What differed | Now |
|---|---|---|---|
| Settled today | **missing** | No surface at all. A head who cleared nine accounts saw the same empty page as one who cleared none | same — new card, from the audit rows; `GET /api/account-check/settled` |
| Account owners | **missing** | No surface at all | same — new card, owner per vertical, open-lead count, created date |
| "New this week" chip | **missing** | — | same — accounts created in the last seven days, counted in the card header |
| Blocked-client chip | differs | Shown in the queue only | same — also on the owners card |
| Thursday-review wording | **missing** | — | same — "listed for the Thursday review; there is no approval step" |

## 3. Waiting on me — `/my-work/tasks`

| Panel | Verdict | What differed | Now |
|---|---|---|---|
| Section | same | Present as "Waiting on me — RFQ steps" | same |
| Layout | differs | A list of rows; step title, RFQ key and timer on one line | same — the demo's table: Lead / Next action / Since / Timer / Act |
| Lead column | **missing** | No lead number or customer, so the row could not be placed | same — `#14 · PAR-2026-0008` over the customer, linked |
| "Since" | **missing** | Only a due date | same — when the step started |
| What closes it | **missing** | — | same — under the step name |
| Timer colour | differs | Page-local `.chain-red` et al. | same — the shared `.rfq-rag` tones; the page-local block is gone |
| Act button | same | — | same |
| With the flag off | same | Block absent entirely | same — asserted character for character by a test |

## 4. Chain board — `/rfqs/chain`

| Panel | Verdict | What differed | Now |
|---|---|---|---|
| "Where RFQs are sitting now" | differs | Twelve grey vertical columns with a count on top — it answered "how many", never "how many are late" | same — one horizontal stacked bar per step, split by timer colour |
| Legend | **missing** | A grey sentence at the foot of the page | same — the demo's five-colour key under the bars |
| Bottleneck callout | **missing** | — | same — "Bottleneck now: X — n of m past the timer" |
| Click a bar to filter | same | — | same |
| The list | differs | One card per RFQ, twelve numbered squares | same — the demo's table with the 20-cell journey strip |
| Journey strip | differs | Twelve boxes, RFQ steps only | same — eight pre-RFQ cells, a lane break, twelve chain cells |
| Owner column | **missing** | "with PAR_SALES" under the card | same — a column, with the person's name |
| "Now" column | differs | A coloured phrase in the card header | same — "With <person> · <step>" over the timer line |
| Quoted column | **missing** | — | same — latest non-draft quote and its revision |
| Priority / status chips | differs | A page-local `.ch-pri` chip | same — the shared chips, "Low / Auction" as the demo words it |
| Held RFQ | differs | Showed red, the colour of lateness | same — purple, with the hold reason |
| Won lead | differs | Read "with Sales · 12. Handover" under a Won chip | same — "Won · handed over" |

## 5. Quotation mail copied to leads@ — `/rfqs/chain#unmatched`

| Panel | Verdict | What differed | Now |
|---|---|---|---|
| The card | **missing** | `GET /api/chain/unmatched` existed and answered; nothing rendered it. The reports page linked `/rfqs/chain#unmatched`, an anchor that was not on the page | same — the demo's card at the foot of the chain board |
| Received / From → to / Subject | **missing** | — | same |
| Match column | **missing** | — | same — "Unmatched" and why, in the demo's wording |
| Link to lead… | **missing** | — | same — a select of open RFQ leads, the suggested lead pre-selected and marked "(same customer)" |
| "Not a quotation" | **missing** | — | same |
| Who may clear it | same | Enforced in the API | same — and the card now says who to ask when you may not |

## 6. The lead record — `/leads/<id>/journey`

| Panel | Verdict | What differed | Now |
|---|---|---|---|
| Header | differs | Company name as an H1, chips in a grey row | same — "Lead #14", the RFQ number in mono, priority and state chips, the demo's meta line |
| Owner | differs | `PAR_SALES` | same — the name |
| Next action | differs | A single line with two dark buttons | same — the demo's action block, the waiting-on-somebody-else note |
| Next action on a closed lead | differs | Said "nothing open on this chain" | same — absent, as in the demo |
| Journey flowchart | differs | Twenty numbered squares in two rows | same — named boxes with a person and a duration under each, arrows between, both lanes |
| Pre-RFQ lane | differs | Eight squares, no explanation when the RFQ came direct | same — marked "skipped — the RFQ came direct" |
| Box detail | differs | A grey panel of `<br>`-joined sentences | same — the demo's card with a meta row |
| Time per step | differs | Bars all starting at the left edge, so nothing showed *when* a step ran | same — a true Gantt: each bar placed at its start, the standard-time mark, a legend |
| Tabs | differs | Journey / Steps & time / Activity / Conditions / Quotes / Audit / Spend | same — the demo's order, and "Spend (IOU)" |
| Steps & time table | **missing** | The tab held the bar chart only | same — the demo's table: #, step, responsible, standard, closed, taken, result |
| Activity form | differs | Stacked labels, a dark button | same — the demo's field grid |

## 7. Conditions — `/leads/<id>/journey#conditions`

| Panel | Verdict | What differed | Now |
|---|---|---|---|
| Conditions by department | differs | A run of `<p>` tags | same — the demo's table: step, by, type, text, visibility |
| "By" column | **missing** | — | same — the person's name |
| Step column | differs | A bare number | same — the step's name |
| "Goes on the quotation" | **missing** | Nothing consolidated anywhere | same — customer-facing terms only, duplicates merged and marked |
| Internal notes | same | Flagged in the list | same — and never in the consolidated list, which a test asserts |

## 8. Quotation versions — `/leads/<id>/journey#quotes`

| Panel | Verdict | What differed | Now |
|---|---|---|---|
| The list | differs | A run of `<p>` tags | same — the demo's table |
| Value | differs | `INR 2500000` | same — ₹25.00 L, the demo's shortening |
| Margin | **missing** | — | same — from the cost sheet on the quote |
| Reason | same | — | same |
| Approved by / when | **missing** | — | same — or "Pending" |
| Sent when / by | **missing** | — | same |
| Status chip | differs | Plain text | same — the shared chip, coloured by status |
| PDF | same | — | same |
| Revision numbering | same | `QUO-… / Rev-01` already | same |

## 9. Spend (IOU) — `/leads/<id>/journey#spend`

| Panel | Verdict | What differed | Now |
|---|---|---|---|
| The table | **missing** | One grey sentence and a button | same — the demo's columns, with "Not set yet — the TMS IOU module, once the CRM-TMS link is built" in place of rows, linked to the readiness panel |
| "Raise IOU in the TMS" | differs | A bare link | same — the demo's action block, the lead number in the sentence |
| Real figures | **cannot be same** | The IOUs are in the TMS and the CRM–TMS link is not built | **Explained, not fixed.** The layout is the demo's; the data says who fills it. Same on the Reports page's spend card. The demo's own numbers are samples |

## 10. Department steps — `/admin/rfq-steps`

| Panel | Verdict | What differed | Now |
|---|---|---|---|
| The page | **missing** | The twelve steps lived in a tuple and a catalogue table, neither with a screen. "Who does step 5, and what closes it?" meant reading Python | same — new admin page |
| Which desk | **missing** | — | same — Sales / Coordinator / SCM / Operations / Vertical Head |
| Who, exactly | **missing** | — | same — the rule, never a person's code, which a test asserts |
| Standard time | **missing** | — | same — the share of the priority window, or the response clock, or the customer's own |
| Closes when | **missing** | — | same — in a sentence; the three that take a click are marked |
| Optional steps | **missing** | — | same — "only when needs technical is ticked at registration" |
| In the catalogue | **missing** | — | same — whether the `task_definitions` row is seeded, and that the chain runs without it |
| Priority windows | **missing** | — | same — the second card |
| When a reason is asked | **missing** | — | same — the third card |

## 11. Reports — `/rfqs/reports`

| Panel | Verdict | What differed | Now |
|---|---|---|---|
| Tiles | differs | Twelve plain tiles, label under the number | same — thirteen, the demo's layout, with the sub-line |
| Sales spend tile | **missing** | — | same — present, reading "not set yet — the TMS" |
| Time per step | differs | A six-column table of ratios | same — the demo's bars with the standard-time mark |
| Slowest-step callout | differs | A yellow box | same — the demo's red-rule callout |
| Delay reasons | differs | Six tables, one per cut (customer, month, person, reason, step, vertical) — the page asked six questions and answered none | same — the demo's two bar groups: why late, what made early |
| By function | **missing** | — | same — the line under the late bars; a new cut on the reasons API |
| Good-practice reasons | **missing** | Collected, never shown | same |
| Workload table | differs | Ten columns, employee codes, no priority split | same — the demo's layout: name, steps, the four priorities, on-time, early, the 98% verdict, gap, open/late |
| Person × step | differs | Column headers were bare step numbers | same — the short step names the sheet uses |
| Heat legend | **missing** | — | same |
| Sales spend card | **missing** | — | same — the demo's two panels, each saying who fills it |

## 12. Audit trail — `/leads/<id>/journey#audit`

| Panel | Verdict | What differed | Now |
|---|---|---|---|
| The list | differs | A run of `<p>` tags | same — the demo's table |
| Columns | differs | When, actor and action run together | same — When / User / Action / Remarks |

---

## Rows that are not "same", in one place

1. **Spend figures, on the lead record and on Reports.** The IOUs
   live in the TMS IOU module and the CRM–TMS link is not built,
   so there is nothing to read. Both screens carry the demo's
   layout with "Not set yet — the TMS IOU module, once the
   CRM–TMS link is built", linked to `/admin/crm-rules`. This is
   the rule the brief set for missing data, not an omission.

2. **The demo's planning sections** — "Decisions taken", "To
   decide", "Second-round feedback", "Order of work", "Account
   first: what the CRM has and what we add" — were not built, as
   the brief instructed.

3. **Dark mode.** The demo carries one; the rest of the CRM is
   light only. Bringing it to five pages would make a half-dark
   portal. Recorded in `QUESTIONS.md`.

4. **IBM Plex.** The demo asks for it; it is not served locally
   and the brief forbids a CDN, so the CRM's own Inter/Barlow
   stays. Recorded in `QUESTIONS.md`.

5. **`/my-work/tasks` at 390 px** still scrolls sideways by
   146 px. It did before this work too (`waiting-on-me--live-
   phone-before.png` is 536 px wide): the cause is the shared
   `_crm_topbar.html`, whose right-hand toolbar does not wrap.
   The five chain pages are fixed, scoped to `.rfq-page`, because
   a global fix to `.crm-top` would change pages whose flags are
   off. My Work is not a chain page and was left as it was.
