# Notifications and scheduled reports

Everything the CRM says to somebody without being asked: the in-app
bell, the notification emails, and the five scheduled reports.

There is one delivery path and one decision table. The path is
`app/services/notify.py`, which writes the bell notification, sends the
email through the existing Microsoft Graph transport, suppresses
duplicates and never raises. The table is `app/services/notification_rules.py`,
whose `MATRIX` is reproduced below. Anything that notifies anybody goes
through both; a notification written anywhere else is a bug.

Report content comes from `app/services/digests.py`, which asks the
Daily Workbench (`app/workbench/service.py`) for the recipient's own
board. The Workbench applies that person's Scope
(`app/access/scope.py`), so a report cannot show somebody a record they
are not allowed to open. The thresholds behind "overdue", "stale",
"due today" and the quote ageing buckets live in
`app/services/sales_rules.py` and are not restated anywhere else.

---

## The matrix

**User** is the record's owner — the primary PIC, or for a quote the
person who prepared it. **Secondary** is the monitor/backup PIC.
**Vertical Head** is the owner's `vertical_head_id` where one is set,
otherwise the active head of their vertical. **Admin** is everyone with
the admin role or the super-admin flag. **Management** is the same
list, unless `REPORT_MANAGEMENT_CODES` names other people.

"On escalation" means that role is added only when the caller says the
record has gone unattended — never automatically at the moment of the
event.

| Event | User | Secondary | Vertical Head | Admin | Management | In-App | Email | Escalation | Status |
|---|---|---|---|---|---|---|---|---|---|
| `lead.assigned` | Yes | — | — | — | — | Yes | Yes | — | Implemented |
| `lead.assigned_secondary` | — | Yes | — | — | — | Yes | Yes | — | Implemented |
| `lead.stage_changed` | Yes | Yes | — | — | — | Yes | — | — | **Not yet** |
| `lead.high_value` | Yes | — | Yes | — | — | Yes | Yes | — | **Not yet** |
| `lead.followup_overdue` | Yes | — | On escalation | — | — | Yes | — | vertical head, after 7 day(s) | Implemented |
| `lead.going_cold` | Yes | — | On escalation | — | — | Yes | — | vertical head, after 14 day(s) | Implemented |
| `rfq.created` | Yes | — | — | — | — | Yes | Yes | — | **Not yet** |
| `rfq.owner_changed` | Yes | — | — | — | — | Yes | Yes | — | **Not yet** |
| `rfq.rate_submitted` | Yes | — | — | — | — | Yes | — | — | **Not yet** |
| `rfq.quote_due_soon` | Yes | — | — | — | — | Yes | — | — | Implemented |
| `rfq.quote_overdue` | Yes | — | On escalation | — | — | Yes | Yes | vertical head, after 2 day(s) | Implemented |
| `quote.submitted_for_approval` | — | — | Yes | On escalation | — | Yes | Yes | admin, after 2 day(s) | **Not yet** |
| `quote.approved` | Yes | — | — | — | — | Yes | Yes | — | **Not yet** |
| `quote.rejected` | Yes (preparer) | — | — | — | — | Yes | Yes | — | **Not yet** |
| `quote.submitted_to_client` | Yes | Yes | — | — | — | Yes | — | — | **Not yet** |
| `quote.won` | Yes | — | Yes | — | Yes | Yes | Yes | — | **Not yet** |
| `quote.lost` | Yes | — | Yes | — | — | Yes | Yes | — | **Not yet** |
| `quote.validity_lapsed` | Yes | — | On escalation | — | — | Yes | — | vertical head, after 7 day(s) | Implemented |
| `account.pic_changed` | Yes | — | — | — | — | Yes | Yes | — | **Not yet** |
| `account.next_action_due` | Yes | Yes | — | — | — | Yes | — | — | Implemented |
| `data.quality_breach` | — | — | — | Yes | — | Yes | Yes | — | **Not yet** |
| `report.daily` | Yes | — | — | — | — | — | Yes | — | Implemented |
| `report.exceptions` | Yes | — | — | — | — | — | Yes | — | Implemented |
| `report.weekly_user` | Yes | — | — | — | — | — | Yes | — | Implemented |
| `report.weekly_head` | — | — | Yes | — | — | — | Yes | — | Implemented |
| `report.monthly` | — | — | — | — | Yes | — | Yes | — | Implemented |

### What "Implemented" means here

A row is **Implemented** when something actually fires it today.

* `lead.assigned` and `lead.assigned_secondary` are delivered by the
  assignment service's own notification code
  (`app/services/lead_assignment.py`), which predates the matrix. The
  rows record the agreed behaviour; moving that code onto `dispatch()`
  is a tidy-up with no change in what anybody receives.
* The rows marked `scheduled` in the source are delivered as part of a
  report rather than as one message per record. A salesperson with
  forty overdue leads would stop reading at the third notification, so
  the overdue, going-cold, quote-due and account-action conditions
  arrive once, grouped, in the morning or evening email.
* Every **Not yet** row needs one line adding at the place the event
  already happens. The row carries that location in its `call_site`
  field, and the whole list can be printed with:

  ```bash
  .venv/bin/python -c "from app.services import notification_rules as n; \
    [print(r['event'], '→', r['call_site']) for r in n.MATRIX if not r['implemented']]"
  ```

---

## Firing an event

```python
from app.services import notification_rules as nrules

nrules.dispatch('quote.approved', quote,
                actor=session.get('emp_code'),
                detail=f'Quote {quote.quote_number} for {quote.total_amount}.')
```

`dispatch` resolves the roles against the record, fills the wording from
the row, and calls `notify.send` once per recipient. It returns
`{emp_code: result}` and never raises, so it is safe on the line after a
commit. It is deliberately *after* the commit: a notification about a
save that then failed is worse than no notification.

Whoever performed the action is not told about their own action unless
the row names the `actor` role explicitly.

Escalation is opt-in per call:

```python
nrules.dispatch('rfq.quote_overdue', rfq, detail='Four days past the '
                'committed date.', escalate=True)
```

---

## The five reports

| Report | Who receives it | When | Contents |
|---|---|---|---|
| Daily action | Everyone active who owns a lead or an account | Mon–Sat, 08:00 IST | Their whole Workbench board, in its own group order |
| End-of-day exceptions | The same people | Mon–Sat, 19:00 IST | Only deadlines missed or imminent, and only where nobody has been in touch today |
| Weekly, per person | The same people | Monday, 08:30 IST | Their week, with every counter against the week before |
| Weekly, vertical head | Everyone marked as a vertical head | Monday, 08:30 IST | The team rollup: who is carrying what, what slipped, high-value deals |
| Monthly management | Administrators, or `REPORT_MANAGEMENT_CODES` | 1st, 09:00 IST | The whole company by vertical and by person, against the previous 30 days |

Nobody is sent an empty report. A person whose board is clear is
skipped, and the run says so; `--send-empty` overrides that for
testing.

### The prior-period comparison

There is no history table behind the Workbench. "A week ago" is the
same rules run against *today's* records with last week's date, so it
answers "how much of this was already late a week ago" and not "what
did the board look like then". Every template that shows a comparison
says so in the small print, because a reader who assumed the stronger
meaning would draw the wrong conclusion about the week's work. A true
week-on-week figure needs a snapshot table, which is a separate change.

---

## Running them by hand

```bash
cd /var/www/procam-crm

# See exactly what one person would receive, send nothing
.venv/bin/python scripts/send_reports.py --daily --to EMP001 --dry-run

# Send one person their report, for real
.venv/bin/python scripts/send_reports.py --daily --to EMP001

# The whole run, as the timer does it
.venv/bin/python scripts/send_reports.py --exceptions
```

One report per invocation, so a failure is attributable. The run prints
a line per recipient — `sent`, `skipped`, `failed`, `dry-run` or
`disabled` — and then the totals. **Exit 0** everyone served, **1** at
least one recipient failed and the rest were still served, **2** bad
arguments. The systemd units treat 1 as success for that reason; the
journal carries the names.

One recipient failing never stops the run: each person is built and
sent inside their own try/except, and a failure rolls back only that
person's session.

## Timers

| Files | Report | IST | UTC (the server's clock) |
|---|---|---|---|
| `procam-crm-daily-report.{service,timer}` | `--daily` | Mon–Sat 08:00 | 02:30 |
| `procam-crm-exceptions-report.{service,timer}` | `--exceptions` | Mon–Sat 19:00 | 13:30 |
| `procam-crm-weekly-report.{service,timer}` | `--weekly-user`, then `--weekly-head` | Mon 08:30 | Mon 03:00 |
| `procam-crm-monthly-report.{service,timer}` | `--monthly` | 1st, 09:00 | 1st, 03:30 |

The templates are in [`deploy/`](deploy/) and are installed the same way
as every other timer there. They are scheduled after the nightly
backup, Copilot index and Data Quality snapshot so that nothing
competes for the database.

The evening unit sets `Persistent=false`: a report about what was still
open yesterday evening, delivered when the machine comes back up at
eleven the next morning, is confusing rather than useful. The others
are `Persistent=true`.

## Configuration

Nothing below is required. The reports work with none of it set, except
that without `CRM_BASE_URL` the buttons in the email are relative paths
and will not open from a mail client.

| Variable | Default | What it does |
|---|---|---|
| `NOTIFY_ENABLED` | on | `false` stops all outbound mail. The run still builds every report and reports what it would have sent, marked `disabled`. |
| `CRM_BASE_URL` | empty | Absolute root for the links, e.g. `https://example.invalid/CRM`. |
| `NOTIFY_FROM` | `CRM_INBOX_EMAIL` | The sending mailbox. |
| `REPORT_MANAGEMENT_CODES` | the administrators | Comma-separated employee codes for the monthly report, for when the people who want it are not the people who administer the system. |
| `REPORT_MAX_RECIPIENTS` | no limit | A ceiling for one run. Useful while the reports are being introduced: set it to 5, read what went out, then remove it. |

Mail needs the **Mail.Send** application permission on the Azure app
registration. That is a separate grant from Mail.Read, and without it
Graph answers 403 and every recipient is reported as `failed`. See the
[Graph setup guide](GRAPH_SETUP_GUIDE.md).

To check the grant without sending anything, run
`.venv/bin/python scripts/check_mail_send.py`; the runbook for the
administrator who has to make the grant is
[GRAPH_MAIL_SEND.md](GRAPH_MAIL_SEND.md).

## Troubleshooting

| What you see | What it means |
|---|---|
| Every recipient `failed`, "the mail server refused it" | Usually the missing Mail.Send grant. Check the journal for the 403. |
| Every recipient `disabled` | `NOTIFY_ENABLED` is off in the environment file. |
| `nobody to send to` | No active employee owns a lead or an account with an email address on file — or, for `--weekly-head`, nobody is marked as a vertical head. |
| One person `failed` with a traceback | Their board would not build. The others were still served; the traceback names the record. |
| A report arrives with fewer records than the screen shows | It is the same board, so check the date: the report is built when the timer runs, and the screen is live. |
| Somebody receives nothing at all | Either their board was empty (`skipped`) or they have no email address in the employee master. |

## Design notes

**Why one table rather than a decision at each call site.** The question
"does the secondary PIC hear about a rejected quote?" used to be
answerable only by reading every route. Now it is one row, and changing
the answer is editing that row rather than finding every place that
sends something.

**Why reports and not per-record notifications.** Conditions that apply
to dozens of records at once — overdue, going cold, no next action —
are grouped into one email. Notifications are for things that just
happened and that one person must act on now.

**Why the reports use each person's own Scope.** Reusing the access
resolver means a report cannot leak, and means the report and the
screen cannot disagree about what somebody owns. There is no second
definition of "my records" to keep in step.

**Why nothing here can break a save.** Both `notify.send` and
`dispatch` swallow every exception. A notification that cannot be
delivered is a nuisance; a lead assignment rolled back because the mail
server hiccupped is a lost enquiry.

---

# Addendum — the 2026-10 email release

The matrix above is still the description of who hears what. Four
things changed around it.

## 1. Delivery is a queue

With `FEATURE_EMAIL_NOTIFY` on, `notify.send` writes a row to
`email_outbox` inside the caller's transaction instead of calling
Graph. `scripts/outbox_worker.py`, on a two-minute timer, claims the
due rows, sends them, and records what happened.

What that buys: a slow mail server no longer makes the CRM slow; a
failed send is retried with a back-off rather than lost; a send cannot
succeed for a transaction that then rolls back; and
`/admin/email/health` can answer "did it go?".

Idempotency is `email_outbox.dedupe_key`, unique in the database — the
same message to the same person about the same record inside ten
minutes is one message, enforced by the index and not by a query.

With the flag off, `notify.send` behaves exactly as it did before.

## 2. The assignment email carries the request

`lead.assigned` and `lead.assigned_secondary` are the only two rules
that attach anything (`attach_original=True`, checked by a test). They
carry the client's original `.eml` and the lead's attachments, inside a
3 MB inline budget; anything over it is left out and the body still
links to the lead. A file that is no longer on disk loses the
attachment, not the email.

## 3. Nothing is emailed outside Procam

`app/services/mail_policy.py`, enforced inside the one transport. See
§6 of the runbook.

## 4. Each person can turn it down

`notification_prefs` holds channels, batching, quiet hours and muted
events, edited at `/me/notifications`. A person with no row behaves
exactly as before the table existed.

## Three new screens

| Screen | Who | What it answers |
|---|---|---|
| `/me/notifications` | everyone | what the CRM tells me |
| `/admin/email/rules` | `admin.email` | who hears what, and what fires it |
| `/admin/email/schedules` | `admin.email` | what runs when, and under which flag |
| `/admin/email/health` | `admin.email` | what is queued, what failed, what was refused |
