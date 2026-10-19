# The task engine — why nothing ever closed, and four changes

19 October 2026. CRM-TASKS-01 to 04 and CRM-AUTOCLOSE-TASKS-01.

---

## 1. The investigation: why no task has closed since 17 August

Read-only. The finding is that **closing was never broken — it was
unreachable.** Five faults, compounding.

### Fault 1 — nothing can close a task by hand

There is no POST, PUT or PATCH route anywhere that completes a task.
`/my-work/tasks` and `/my-work.json` are both GET. The screen lists
work and offers no way to finish it. Every closure therefore has to
be automatic, and the automatic paths are faults 2 to 5.

### Fault 2 — the lead edit form never called the task engine

Tasks close through `task_engine.on_state_change()`, fired by
`_fire_task_hook`. That hook is wired into the lead *advance*
endpoint and the lead *decision* endpoint — and **not** into
`PUT /api/leads/<id>`, which is the ordinary lead edit form and
which sets `stage` through its `fields_map` like any other field.

The form is how most people change a stage. Every one of those
changes was invisible to the task engine.

### Fault 3 — two definitions can never close, by construction

| Task | Open | Completion state | Reachable? |
|---|---|---|---|
| `deal.lost.debrief` | 1,156 | **none** | never |
| `competitor.log` | 1,156 | `Opportunity / Competitor Logged` | never — the six opportunity stages are Qualification, Needs Analysis, Proposal Sent, Negotiation, Closed Won, Closed Lost |

That is 2,312 of the 3,659 original tasks that could not close
whatever anybody did.

### Fault 4 — every escalated twin was immortal

The SLA sweeper escalated a task by writing a **second row** with
`task_key = '<key>.escalated'`. Tasks close by matching a
`TaskDefinition`'s `completion_state`, and `'<key>.escalated'`
matches no definition — so from the moment each twin was written, no
code path existed that could ever close it. 3,659 of them, exactly
half the backlog.

The sweeper's own duplicate check was also looking for the wrong
thing: it searched for a row with the *original* key and the
escalation role while creating one with the `.escalated` key, so it
could never find what it had made. It avoided running away only
because the outer loop reads `Pending` and the row it had just
escalated was no longer `Pending`.

### Fault 5 — a closed lead closed nothing behind it

No definition names Won, Lost, On Hold or Not Interested as a
completion state. On 19 October, **2,262 of 2,694 open Lead tasks sat
on a lead that was already closed.**

### What was ruled out

* The SLA sweeper is running — `procam-crm-sla.timer`, every 15
  minutes, last fired normally.
* No `task_engine` errors in the journal over 30 days. Nothing is
  failing; nothing is being attempted.
* Creation is not runaway: 1,449 on 6 September from the backfill
  script, small daily numbers after, then the 10–12 October bursts
  which are the twins.

### Still open — your call

Faults 4 and 5 are fixed below. **1, 2 and 3 are not**, because the
instruction was to find the cause before changing anything:

1. Should there be a "Done" button? Without one, tasks can only close
   as a side effect of something else.
2. Should `PUT /api/leads/<id>` fire the whole task hook? It now
   closes tasks on a closed lead (below), but it still does not
   *create* tasks, so a stage change made on the form raises nothing.
   Turning that on would start generating tasks from the busiest
   write path in the CRM, which wants deciding, not assuming.
3. `deal.lost.debrief` needs a completion state, and
   `competitor.log` needs one that exists.

---

## 2. Escalation is a column, not a row

`escalated`, `escalated_to`, `escalated_at` on `task_instances`. An
escalation is something that happened to a task, not a second task;
two rows also meant two owners could each think the other had it.

```
.venv/bin/python scripts/2026_10_19_task_escalation.py --check
.venv/bin/python scripts/2026_10_19_task_escalation.py
.venv/bin/python scripts/2026_10_19_task_escalation.py --check
```

Then the merge — one transaction, one audit row per twin naming the
row that went:

```
.venv/bin/python scripts/merge_escalated_tasks.py --check
.venv/bin/python scripts/merge_escalated_tasks.py --apply
```

The original keeps its id, its history and its owner. A twin with no
surviving original is **left alone and listed**: deleting it would
lose the only record that the work was raised, and there is nothing
to merge it into.

`scripts/task_sla_sweep.py` now escalates in place. Re-running finds
`escalated` already true and does nothing — which is what the old
duplicate check was trying and failing to do.

---

## 3. Overdue split into leads and tasks

One tile cannot answer both "how far behind is my pipeline" and "how
big is the task backlog". While it tried, it answered neither: the
combined number was 7,475 for an admin against 146 overdue leads.

`overdue_leads` and `overdue_tasks` are now two tiles on both the
Workbench and the Overview panel, each filtering to its own kind.
The old `overdue` key stays in the registry with no tile and no chip
so a saved link still resolves; the two halves add up to it exactly,
and a test says so.

---

## 4. Task backlog clean-up — DRY RUN ONLY

```
.venv/bin/python scripts/task_backlog_report.py --check
```

Two rules:

* **lead closed** — an open task on a lead already Won, Lost, On Hold
  or Not Interested.
* **expired — lead no longer live** — an open task older than four
  months whose lead is also older than four months.

Rule (a) wins where both apply: "the lead closed" is the truer reason
and the one somebody reading the row later can check.

Counts by owner and by task type are in the output. **Nothing is
written.** `--apply` refuses without `--i-have-approval`, because the
instruction was to report and stop.

---

## 5. CRM-AUTOCLOSE-TASKS-01

The four named tasks:

```
.venv/bin/python scripts/close_named_tasks.py --check
.venv/bin/python scripts/close_named_tasks.py --apply
```

| id | key | lead | stage |
|---|---|---|---|
| 1342 | `lead.rfq_review` | Lead#11531 Siemens Energy India | Won |
| 1541 | `lead.rfq_review.escalated` | Lead#11531 | Won |
| 1283 | `deal.negotiation` | Lead#11518 SIEMENS LIMITED | Won |
| 1536 | `deal.negotiation.escalated` | Lead#11518 | Won |

Two original/twin pairs, both on Won leads — the exact pattern items
2 and 5 address generally.

**From now on, closing a lead closes its open tasks in the same
transaction.** `app/services/task_autoclose.py`, wired into
`_fire_task_hook` (so the advance and decision endpoints carry it)
and directly into `PUT /api/leads/<id>` (which has never called the
task engine at all — fault 2).

Tasks are marked **Cancelled**, not Completed: nobody completed them.
The reason goes into `notes`, `completed_at` records when, and each
gets an audit row. The service does not commit — it runs in the
caller's transaction, so a lead marked Won and the tasks that close
with it land together or not at all.

---

## Deploy

Push first; then on the server:

```
cd /var/www/procam-crm
git pull
.venv/bin/python scripts/2026_10_19_task_escalation.py --check
.venv/bin/python scripts/2026_10_19_task_escalation.py
.venv/bin/python scripts/2026_10_19_task_escalation.py --check
sudo systemctl restart procam-crm
```

Then, in this order — the named four first, so the merge sees them
already closed rather than closing them by a different route:

```
.venv/bin/python scripts/close_named_tasks.py --check
.venv/bin/python scripts/close_named_tasks.py --apply
.venv/bin/python scripts/merge_escalated_tasks.py --check
.venv/bin/python scripts/merge_escalated_tasks.py --apply
.venv/bin/python scripts/task_backlog_report.py --check
```

The last one writes nothing and is the report to bring back.
