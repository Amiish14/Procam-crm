# Daily Workbench — what it shows and why

The Workbench answers three questions on one screen: what needs
attention today, what is about to become overdue, and what information
is missing. It is at **/CRM/my-work**; managers also have
**/CRM/team-workbench** across the people their data scope reaches.

Everything on it comes from `app/services/sales_rules.py`. Change a
threshold there and the board, the ageing buckets and (as they are
built) the emails and reviews all move together.

---

## 1. The action matrix

One row per condition. A record can trip several; it is filed under the
first group in this order, and the others still show as reasons.

| Condition | Record | Why it is work | Due date | Priority | Group |
|---|---|---|---|---|---|
| `followup_overdue` | Lead | The date set for the next contact has passed | the follow-up date | Overdue | Action required today |
| `followup_today` | Lead | Promised contact today | today | Today | Action required today |
| `quote_overdue` | RFQ → Lead | Past the quote deadline | RFQ `quote_by_date`, else received + 3 days | Overdue | Quotations |
| `quote_due_soon` | RFQ → Lead | Quote expected within 2 days | same | Today | Quotations |
| `quote_missing_detail` | Lead | Marked Quoted with no quote value or date, so it is invisible to quote reporting | — | Soon | Quotations |
| `quote_lapsed` | Lead | Quote validity passed while still open | validity date | Overdue | Quotations |
| `negotiation_idle` | Lead | Under Negotiation and quiet for 7 days | — | Soon | Customer follow-up |
| `idle` | Lead | No contact for 7 days | — | Soon | Customer follow-up |
| `stale` | Lead | No contact for 30 days | — | Soon | Stale opportunities |
| `no_next_action` | Lead | Nothing scheduled, so nothing will remind anyone | — | Routine | Data update required |
| `data_issue` | Lead | A Data Quality check flags it (see §3) | — | Routine | Data update required |
| `newly_assigned` | Lead | Arrived in the last 48 hours | — | Routine | Newly assigned |
| `high_value` | Lead | Worth ₹1 crore or more | — | Soon | High-value priorities |
| `account_next_action` | Account | The development plan has an action due | `next_action_at` | Today | Account development |
| `account_quiet` | Account | No activity for 90 days | — | Routine | Account development |
| open task | TaskInstance | The task engine raised it | task `due_at` | by due date | by task type |

**Thresholds** (`app/services/sales_rules.py`): idle 7 days, stale 30
(Data Quality's `NO_CONTACT_DAYS`), newly assigned 48 hours, high value
₹1 crore, quote standard 3 days, account quiet 90 days.

**"Contact"** means an activity or a customer email —
`app/services/contact.py`, never `updated_at`. A lead nobody has ever
contacted is judged on its own age, so one created this morning is not
stale and one created two months ago is.

**"Today"** is the business day in India (`business_today()`). The
server runs UTC, and comparing a business date against UTC made "due
today" and "days left" read a day early every evening after 18:30 IST.

---

## 2. Quote ageing

Buckets, as the dashboard shows them: **Today · 1–3 · 4–5 · 6–10 · more
than 10 days**, measured from the RFQ's `received_date`. **Overdue** is
counted separately — it is a missed deadline, not an age.

The deadline is the RFQ's own `quote_by_date` where there is one;
otherwise the service standard of 3 days after arrival. The two are
distinguished in the API by `deadline_committed`, so an assumed date is
never shown to anyone as a promise made to the customer.

---

## 3. Data issues

The Workbench does not define its own hygiene rules. It runs the
lead-level Data Quality checks for the viewer's scope and shows what
they flag: `unowned_leads`, `leads_of_leavers`, `stale_leads`,
`leads_no_followup`, `unlinked_leads`, `lead_unknown_vertical`,
`quoted_without_quote`, `won_leads_no_value`, `quote_past_validity`,
`lead_account_mismatch`.

---

## 4. Bulk updates

Select records, choose an action, **preview**, give a reason, apply.

| Action | Writes | Validation |
|---|---|---|
| Set the next follow-up date | `followup_date` | a real date, not past, within a year |
| Set the next action | `next_action` | 1–200 characters |
| Mark the follow-up done | clears `followup_date` | skipped where none was due |
| Assign an owner | through `lead_assignment.assign` | must be an active employee |
| Change the stage | `stage` | must be a known stage |
| Set the vertical | `procam_vertical` | must be a Master Data vertical |
| Log the same activity | a `lead_activities` row | 1–200 characters |
| Archive | `is_archived` | reversible; skipped where already archived |

The safety model is Data Quality's: the preview is signed (HMAC over
exactly what would change), applying re-plans from the database and
refuses if anything moved, and **each record is saved in its own
savepoint** — two failures out of forty leave thirty-eight saved and
report the two by name, still selected for a retry. At most 200 records
per request; the page sends larger selections in batches.

Every batch writes one `workbench.bulk` audit event carrying the reason,
and each record's own field change is audited by the usual listener.

---

## 5. Who sees what

| Surface | Rule |
|---|---|
| `/my-work` | the signed-in person's own records, by Access Matrix scope |
| `/team-workbench` | everyone the viewer's scope reaches; refused (403) to someone whose scope is only themselves |
| `?for=CODE` | narrows within the viewer's scope; a code outside it is **refused**, not silently ignored |
| bulk preview/apply | every id is re-checked against scope; one id outside it refuses the whole batch |
| reminders | only to someone the sender's scope reaches |

Tested, including by removing each guard and confirming the tests fail
(`tests/test_workbench.py`).

---

## 6. Notifications — what exists today

One service writes them: `app/services/notify.py` (in-app always, email
through the existing Graph transport when asked, duplicate suppression
within 10 minutes, never raises into the caller's transaction).

| Event | Recipient | In-app | Email | Escalation | Status |
|---|---|---|---|---|---|
| Lead assigned / monitoring | new primary, new secondary | yes | yes | — | existing (`lead_assignment`) |
| Task raised for a person | task owner | yes | no | SLA sweep escalates the task | existing (task engine) |
| Workbench reminder | the colleague a manager nudges | yes | optional | — | **new** |
| RFQ received, quote due, quote overdue, quote submitted, won, lost, stale negotiation, high-value lead, follow-up due/overdue, account inactive, missing data, new external project | — | — | — | — | **not built** (Release 2) |
| Daily action report, end-of-day exceptions, weekly user, weekly vertical head, monthly management | — | — | — | — | **not built** (Release 2) |

The conditions those emails need already exist in the action matrix, so
Release 2 is a scheduled job over the same service, not new rules.

---

## 7. Known differences this does not resolve

The CRM defines some KPIs more than once, and this release deliberately
does not change the existing surfaces:

- **"Open"** — four rules: `STAGES_PIPELINE` (dashboard, KPI targets,
  and the Workbench), `NOT IN STAGES_TERMINAL` (`/api/my-work`),
  `NOT IN ('Won','Lost')` (PIC 360, `/api/stats` team block),
  and the Copilot's `_TERMINAL` which also knows `Closed Won/Lost`.
- **Win rate** — won/all leads (dashboard), won/(won+lost)
  opportunities (PIC 360, Company 360), won/decided in a window
  (Copilot), won/RFQ count (`rfq_won_pct`).
- **Pipeline value** — lead-based (dashboard) vs opportunity-based
  (Copilot, funnels).
- **Ageing buckets** — 0-7/8-15/16-30/31-60/60+ (dashboard),
  0-7/8-30/31-90/90+ (funnels), hours (triage), and the quote buckets
  above.
- **`profile_sent`** — a stage list on the dashboard, `intro_mail_date`
  in the KPI targets engine.
- **KPI target scope leak** — `won_value` and `pipeline_value` actuals
  drop the vertical/user filter (`app.py`, `_kpi_actual_for`).

Each is a behaviour change for an existing report, so each needs a
decision before it is unified.
