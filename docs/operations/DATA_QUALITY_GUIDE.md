# Data Quality Guide

For **CRM Administrators**, who keep the data clean, and **Business
Owners**, who decide what "clean enough" means and follow the trend.

The Data Quality module answers three questions:

1. What is wrong with the data right now, and what does each problem cost?
2. Which records are they, and how do I fix them?
3. Is it getting better?

It never changes data on its own. Every number is a live measurement, and
every correction is a decision an administrator makes, previews and signs
with a reason.

---

## 1. Where it is and who sees what

| Screen | Address |
|---|---|
| Dashboard | **/CRM/admin/data-quality** (Admin → Data Quality) |
| One check's records | **/CRM/admin/data-quality/&lt;check&gt;** |
| CSV of one check | **/CRM/admin/data-quality/&lt;check&gt;/export.csv** |
| JSON for integrations | **/CRM/api/data-quality** and **/CRM/api/data-quality/&lt;check&gt;** |

**Permission.** The module needs the **Master Data** permission
(`admin.master`) in Access Control — the same gate it has always had.

**Scope.** Every count and every row is measured inside the viewer's
data scope from the Access Matrix:

| Viewer's scope | What they see |
|---|---|
| Whole company | Every problem in the CRM, plus the company-wide trend sparklines |
| Own vertical | Problems in records owned by people in their vertical or reporting to them |
| Own records | Problems in records they own |

Consequences worth knowing:

* An unowned lead belongs to nobody, so it only appears for a
  whole-company viewer. The same goes for anything with no owner of its
  own: emails or notes whose lead was deleted, classifier decisions, and
  company names waiting in Data Mapping.
* Duplicate accounts are compared only among the accounts the viewer can
  see — telling someone their account matches one they cannot open would
  reveal it.
* Trends are company-wide totals, so the sparklines are shown only to
  whole-company viewers.

---

## 2. Reading the dashboard

Each card shows:

* **The count** — click it to open the records.
* **Severity** — how much damage the problem does (below).
* **Sparkline** — the company-wide count over the last 30 daily snapshots
  (whole-company viewers, once the snapshot job has run on two days).
* **What it costs** — one sentence on the business consequence.
* **Fix** — the concrete next step.
* **Go and fix** — the screen where the fix is normally made.
* **batch fix available** — the detail page offers a batch correction.

Cards are ordered worst first: by severity, then by count.

| Severity | Meaning | Expected response |
|---|---|---|
| **critical** | Work is invisible or blocked: nobody owns it, or it cannot move to operations | Same week |
| **high** | Revenue or customers are at risk: neglected enquiries, stale deals, misfiled quotes | Within the month |
| **medium** | Reports and routing are wrong: missing dates, unlinked or mislinked records | Planned clean-up |
| **low** | Learning or long-term hygiene | Review quarterly |

---

## 3. The checks

`Batch fix` lists the corrections available on the check's detail page.
"—" means each record needs a person's judgement and no batch tool is
offered.

### Ownership

| Check | Severity | Why it matters | How to fix | Batch fix |
|---|---|---|---|---|
| `unowned_leads` — Leads with no owner | critical | They never appear in anyone's My Work, so no task, reminder or escalation reaches them. | Assign an owner; archive what is dead. Also Admin → Bulk Leads. | Assign owner, Archive |
| `leads_of_leavers` — Leads owned by someone who has left | critical | The enquiry sits with an inactive employee. | Reassign to an active colleague, or archive. | Assign owner, Archive |
| `unowned_opps` — Opportunities with no owner | critical | Nobody progresses or forecasts the deal. | Assign an owner. | Assign owner |
| `opps_of_leavers` — Opportunities owned by someone who has left | high | The deal belongs to someone who cannot act on it. | Reassign. | Assign owner |
| `no_pic` — Accounts with no owner | high | Nobody owns the relationship; new leads cannot be routed. | Assign an owner, or Accounts → Owners. | Assign owner |
| `accounts_of_leavers` — Accounts owned by someone who has left | high | New leads route to an inactive employee. | Assign an active owner. | Assign owner |
| `tasks_no_owner` — Tasks with no owner | critical | Appear for nobody. | Reassign in My Work. | — |
| `tasks_of_leavers` — Tasks owned by someone who has left | high | Never completed or escalated. | Reassign in My Work, or reassign the record they belong to. | — |

"Left" means the owner's employee code is not an **active** employee in
the Employee Master.

### Activity and pipeline

| Check | Severity | Why it matters | How to fix | Batch fix |
|---|---|---|---|---|
| `stale_leads` — Open leads with no contact in `NO_CONTACT_DAYS` | high | An enquiry nobody called or emailed goes to a competitor who did. | Contact the customer and log it; set a follow-up; archive what is dead. | Follow-up date, Assign owner, Archive |
| `leads_no_followup` — Open leads with no follow-up, or one overdue | medium | Nothing reminds the owner. | Set a follow-up date. | Follow-up date |
| `stale_opps` — Stale opportunities | high | Deals past their close date, or untouched for `STALE_OPPORTUNITY_DAYS`, inflate the forecast. | Update the stage, or set a realistic close date. | Close date, Assign owner |
| `opps_no_close_date` — Open opportunities with no close date | medium | Left out of every forecast by month. | Set the expected close date. | Close date |
| `rfq_no_quote` — RFQs past their quote-by date with no quote | high | The customer's deadline passed with no price recorded. | Raise the quote, or mark the RFQ Lost or Withdrawn. | — |

Definitions:

* **Open lead** — not archived, stage not Won / Lost / On Hold / Not
  Interested.
* **Contact** — an activity or an email on the lead (the CRM's single
  definition in `app/services/contact.py`, also used by Sales Intelligence
  and the Copilot). Editing a field is not contact. A lead younger than
  the threshold is not listed.
* **Open opportunity** — stage not Won / Closed Won / Lost / Closed Lost /
  On Hold / Not Interested, and neither won nor lost date set.
* **Untouched opportunity** — no update to the deal and no contact on its
  lead within the threshold.

### Won deals

| Check | Severity | Why it matters | How to fix | Batch fix |
|---|---|---|---|---|
| `won_no_po` — Won deals with no customer PO yet | critical | The Project and Job are created against the PO; without it the deal cannot move. | Record the PO in Handovers → Awaiting PO. | — |
| `dupe_po_refs` — One PO on more than one handover | critical | Two projects against one customer order. | Correct the PO on the wrong handover, or cancel it. | — |
| `won_no_handover` — Won deals with no handover | low | Operations have nothing to act on. | Create the handover. | — |
| `won_no_value` — Won deals with no value | medium | Count as zero in every value report. | Enter the won value. | — |
| `won_leads_no_value` — Won leads with no value | high | Won-value, win-rate by value and margin leave them out. | Add the quote or opportunity value on the lead. | — |
| `quoted_without_quote` — Quoted leads with no quote value or date | high | Quote-to-win, quote ageing and pipeline value read the quote. | Fill the lead's Quote block; confirm an email suggestion if one is offered. | — |
| `quote_past_validity` — Open quotes past their validity | medium | The customer holds a price Procam no longer stands behind. | Follow up and re-quote, or move the lead on. | — |
| `lost_no_competitor` — Lost deals with no competitor | low | No competitive learning from the loss. | Record who won on the Competitors tab. | — |

For `won_no_handover`, a deal counts as won when its stage says so **or**
its won date is set — a deal somebody forgot to move to Won is still won.
Cancelled handovers are ignored by both handover checks.

### Linking and duplicates

| Check | Severity | Why it matters | How to fix | Batch fix |
|---|---|---|---|---|
| `unlinked_opps` — Opportunities not linked to an account | critical | Excluded from every account report. | Choose the account on each deal. | — |
| `unlinked_leads` — Leads not linked to an account | medium | Missing from Company 360. | Link where the name matches exactly one account; decide the rest in Data Mapping. | Link to account |
| `pending_mappings` — Company names awaiting a decision | medium | Every record carrying the name stays unlinked. | Decide on the Data Mapping screen. | — |
| `dupe_companies` — Duplicate accounts | medium | History, owners and value split across two records. | Merge with `scripts/2026_09_11_company_dedup.py` (previews, reversible). | — |
| `dupe_contacts` — Duplicate contacts | medium | Notes land on either record at random. | Keep one record per person, deactivate the other. | — |

Duplicates are grouped by what matched:

* **Accounts** — the same normalised name (case, punctuation and words
  like Pvt, Ltd, Limited, Company, India ignored), the same GSTIN, or a
  shared mail domain.
* **Contacts** — the same email (case ignored), or the same phone or
  mobile (last ten digits).

### Integrity

| Check | Severity | Why it matters | How to fix | Batch fix |
|---|---|---|---|---|
| `orphan_records` — Records pointing at a lead or account that is gone | medium | Invisible — and SQLite gives a deleted id to the next new lead, which then inherits them. | Report to the CRM administrator; repaired by script, never in bulk. | — |
| `broken_relationships` — Broken relationships | medium | A deal whose lead belongs to another account, or a contact whose account is gone, is reported under the wrong customer. | Correct the account on each record. | — |
| `empty_mandatory` — Mandatory fields left empty | medium | Lead with no company or stage, deal with no stage, account with no name. | Fill in the field. | — |
| `lead_account_mismatch` — Lead name does not match its account | medium | The lead shows on another customer's Company 360. | Relink, or confirm it is a trading name. | — |
| `lead_unknown_vertical` — Leads with a vertical not in Master Data | medium | Escapes vertical reports and vertical-scoped views. | Set a vertical, or add the missing one to Master Data first. | Set vertical |
| `account_unknown_vertical` — Accounts with a vertical not in Master Data | medium | Leads route to a desk that does not exist. | Set a vertical. | Set vertical |

The vertical checks compare against the **active** items of the Vertical
list in Master Data (label or code). If that list is empty they report
nothing — an unconfigured list is a setup task, not thousands of data
problems.

### Email intake

| Check | Severity | Why it matters | How to fix | Batch fix |
|---|---|---|---|---|
| `email_leads_non_lead` — Email leads the classifier says are not leads | high | Supplier mail, replies and internal mail in the pipeline waste time and distort conversion. | Archive if not an enquiry, or correct the classification in Lead Review. | Archive, Assign owner |
| `review_backlog` — Leads stuck in review over `REVIEW_BACKLOG_DAYS` | medium | An enquiry waiting for a reviewer is unanswered. | Work the Lead Review queue. | — |
| `classification_orphans` — Classifications pointing at a deleted lead | low | The learning engine counts a lead that is gone. | Decide again in Lead Review. | — |
| `quotes_filed_as_sent` — Quotes received filed as sent | high | An agent's price recorded as Procam's may have set the stage to Quoted with the wrong amount. | Check each lead's stage and quoted amount. | — |

### Customers

| Check | Severity | Why it matters | How to fix | Batch fix |
|---|---|---|---|---|
| `inactive_customers` — Accounts inactive for `INACTIVE_CUSTOMER_MONTHS` | low | A customer nobody has spoken to in a year is being served by someone else. | Plan an account review, or mark the account inactive. | — |

Inactive means: the account is older than the threshold and, within it,
has no new or updated lead, no new or updated opportunity, no contact on
any of its leads, and no recorded account activity.

---

## 4. Thresholds

All thresholds live in one place, **`app/data_quality/definitions.py`**,
which both the dashboard and the read-only CSV report read. Changing one
is a code change reviewed like any other.

| Constant | Default | Used by |
|---|---|---|
| `NO_CONTACT_DAYS` | 30 | `stale_leads` |
| `STALE_OPPORTUNITY_DAYS` | 45 | `stale_opps` |
| `INACTIVE_CUSTOMER_MONTHS` | 12 (counted as 30-day months) | `inactive_customers` |
| `REVIEW_BACKLOG_DAYS` | 7 | `review_backlog` |
| `BATCH_MAX` | 500 | records per batch correction |
| `PREVIEW_TTL_SECONDS` | 600 | how long a preview can be applied |
| `TREND_POINTS` | 30 | snapshots drawn in a sparkline |
| `PAGE_SIZE` | 100 | records per detail page |
| `EXPORT_MAX` | 20,000 | rows in one CSV export |
| `MAX_FUTURE_DAYS` | ~3 years | furthest follow-up or close date accepted |

---

## 5. Batch correction — the safety model

Batch correction exists so that clearing 200 leads of a departed
colleague is ten minutes' work instead of an afternoon. It is built so
that it cannot do anything the administrator did not see and approve.

### What it can do

| Correction | Applies to | Writes |
|---|---|---|
| Assign an owner | leads, opportunities, accounts | owner (leads: via the Bulk Leads service, with assignment history) |
| Set the follow-up date | leads | follow-up date |
| Set the expected close date | opportunities | expected close date |
| Set the vertical | leads, accounts | vertical, from the Master Data list only |
| Archive | leads | archived, via the Bulk Leads archive — reversible from Bulk Leads |
| Link to the one account with this name | leads | account link, only where exactly one active account has the lead's company name (case and outer spaces ignored); anything else is left unchanged and listed |

It can **never** delete a record, and it is offered only on the checks
listed in section 3.

### How it works

1. **Select.** On a check's detail page, tick the records (up to 500).
2. **Choose** one correction and its value (an active employee, a date
   from today onwards, or a Master Data vertical).
3. **Preview.** The CRM shows every selected record with the field, its
   current value and the value it will have. Records that would not
   change are listed separately with the reason. Nothing has changed yet.
4. **Reason.** Write why — it is stored with the batch and with every
   individual change.
5. **Apply** and confirm.

### The guarantees

* **What is applied is what was previewed.** The preview is signed with
  the server's secret key over the check, the correction, the value, the
  administrator and every record's before-and-after. Applying recomputes
  the changes from the database and refuses unless they match exactly. If
  anyone edited one of those records in between, or the request asks for
  a different value or more records, **the whole batch is refused and
  nothing changes** — preview again.
* **Previews expire after 10 minutes**, and belong to the person who made
  them: another administrator cannot apply your preview.
* **Scope is checked on every record, twice** — at preview and at apply.
  A single record outside your scope refuses the whole batch. The message
  is the same whether the record is outside your scope or does not exist,
  so the tool cannot be used to discover records.
* **Only records that still have the problem** can be corrected. If a
  colleague fixed some in the meantime, the batch is refused — reload and
  select again.
* **Applying twice does nothing.** The first apply resolves the problem,
  so the same preview is refused the second time.
* **Everything is audited** (Admin → Audit Trail, **/CRM/admin/audit**):
  * one `data_quality.batch_fix` event per batch: who, when, the check,
    the correction and value, how many records, every before and after,
    and the reason;
  * the usual per-record events (`lead.ownership_change`,
    `opportunity.update`, `company.ownership_change`, …) carrying the same
    reason;
  * archives also write the Bulk Leads deletion-audit snapshot.
* **CSV exports are audited** too (`data_quality.export`).

---

## 6. Exports

**Export CSV** on a detail page downloads every record of that check that
your scope reaches (up to 20,000 rows): check, kind, id, name, details
and a link. Duplicate checks add the group.

Every cell is made safe to open in Excel or LibreOffice: text that would
start a formula (`=`, `+`, `-`, `@`) is prefixed with an apostrophe, so a
customer name like `=HYPERLINK(...)` opens as text.

The file contains customer names and contact details. Treat it like the
CRM itself: do not email it or attach it to tickets.

---

## 7. Trends and the daily snapshot job

The sparklines come from **`data_quality_snapshots`**: one row per check
per day holding the company-wide count (no customer data).

The job:

```bash
.venv/bin/python scripts/data_quality_snapshot.py          # today
.venv/bin/python scripts/data_quality_snapshot.py --date 2026-09-30
```

* **Idempotent** — running it again on the same day replaces that day's
  counts, so a retried timer cannot draw a spike.
* A check that fails is stored as a **gap** (no count), not a zero; the
  other checks are still written.
* Exit codes: `0` all measured · `1` written, some checks failed (named in
  the output) · `2` nothing written (the table does not exist yet, or a bad
  `--date`).
* It never creates or alters tables. Until the `data_quality_snapshots`
  migration is applied it prints that and exits `2`; the dashboard simply
  shows no sparklines.

Install the daily timer (templates in `docs/operations/deploy/`, runs at
03:00 after the backup and the Copilot index):

```bash
sudo cp docs/operations/deploy/procam-crm-dq-snapshot.service \
        docs/operations/deploy/procam-crm-dq-snapshot.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now procam-crm-dq-snapshot.timer
sudo systemctl start procam-crm-dq-snapshot.service     # run once now
journalctl -u procam-crm-dq-snapshot.service -n 40
```

Check the unit's `User=` and `WorkingDirectory=` against
`systemctl show procam-crm -p User -p WorkingDirectory` first, as for the
other templates.

A sparkline needs at least two days of snapshots.

---

## 8. The read-only CSV report

`scripts/data_quality_report.py` (see the Administrator Guide) writes 13
CSVs for review meetings. It opens the database read-only and does not
import the application, so it cannot change anything. It reads the same
thresholds and name normalisation as the dashboard, and a test holds the
two to the same records for the checks they share:

| Report CSV | Dashboard check(s) |
|---|---|
| `accounts_without_owner` | `no_pic` + `accounts_of_leavers` |
| `leads_without_owner` | `unowned_leads` + `leads_of_leavers` |
| `opportunities_without_owner` | `unowned_opps` + `opps_of_leavers` |
| `opportunities_overdue_close` | part of `stale_opps` |
| `won_without_po` | `won_no_po` (handover has no PO) + `won_no_handover` (no handover at all) |
| `won_without_handover` | `won_no_handover` |
| `duplicate_accounts` | `dupe_companies` |
| `duplicate_contacts` | `dupe_contacts` |
| `quotes_received_filed_as_sent` | `quotes_filed_as_sent` |

The report runs without scope (it is a server-side tool); the dashboard
always applies the viewer's scope.

---

## 9. For Business Owners: a monthly routine

1. Open the dashboard as a whole-company viewer. Read the critical row
   first; any critical count that is not falling on its sparkline needs an
   owner and a date.
2. For each high check, agree who clears it and by when. Neglected leads
   (`stale_leads`) and stale deals (`stale_opps`) are sales-team work, not
   administrator work — send the owners their own view (they see only
   their records).
3. Watch the trend, not the number: a steady count means new problems are
   arriving as fast as old ones are fixed, which points at a process gap
   (for example, leads arriving without an account owner to route to).
4. Review low checks quarterly.

## 10. Troubleshooting

| Symptom | Cause and action |
|---|---|
| A card shows "—" and "Could not check" | That check failed; the rest of the page is unaffected. Report the message. |
| No sparklines | You are not a whole-company viewer; or the snapshot job has not run on two days; or the migration is not applied (the job exits `2`). |
| An administrator sees fewer problems than expected | Their Access Matrix scope is narrower than whole company. That is intended. |
| "…not available to you. Nothing was changed." | A selected record is outside your scope (or no longer exists). Select only records on your page. |
| "…no longer have this problem" | Someone fixed some of them. Reload and select again. |
| "This is not what was previewed…" | A record changed after the preview, or the request differs. Preview again. |
| "The preview has expired" | More than 10 minutes passed. Preview again. |
| "Your session expired — refresh the page" | The security token lapsed. Refresh and redo the preview. |
