# The three faults, decided and built

19 October 2026. CRM-TASKS-03 (fault 3), the Done button (fault 1),
and the lead form firing the engine (fault 2). The faults themselves
are described in `RELEASE_2026_10_TASKS.md`.

---

## Fault 3 — the two tasks that could never close are retired

`deal.lost.debrief` and `competitor.log` were raised on every lost
deal, 1,156 each, and neither could ever be closed: one has no
completion state at all, the other waits on an Opportunity state
(`Competitor Logged`) that is not in the vocabulary. Between them they
are 2,312 of the 2,528 open tasks and all of the growth — a new pair
every time an opportunity is lost.

**The questions were right; the mechanism was wrong.** A task says "go
and find out later". The Lost dialog catches the person at the moment
they already know, for the price of one dropdown and one line of
text.

### What the dialog asks now

* **Lost to (competitor)** — optional, from the competitor list, with
  "+ Add a competitor…".
* **Debrief note** — optional, short text (2,000 characters), and
  still required when the reason is *Other*, as it already was.

Both optional. A required field people cannot answer is how 4,738 of
the 7,119 lost leads came to have **no lost reason at all**.

Stored on the lead as `lost_to_competitor_id` (FK to
`competitor_masters`) and `debrief_note`.

### Two things that were already broken in that dialog

Found while rebuilding it, and fixed:

* the **Competitor box was collected and never sent** — it existed,
  people could type into it, and the value went nowhere;
* **`remarks` was sent and never read** by `/api/leads/<id>/decision`,
  which only ever looked at `outcome`, `reason` and `value`.

Same class as the Won value, which the comment in that route records
as "collected and never sent". The Lost branch now sends the two
fields the server actually stores.

### On Hold had the same hole

The On Hold dialog has always collected a remark and sent it as
`remarks`, which the decision route never read — so the one sentence
that explains a paused deal was discarded at the moment somebody took
the trouble to type it. It is stored now as `leads.hold_note`, and
`remarks` is still accepted so a tab cached before today does not
lose the typing a second time.

`lost_to_competitor_id`, `debrief_note` and `hold_note` are all in
the audit listener's watch list, so a change to any of them carries
an audit row like any other field. These are the only record of why
a deal ended; they should not be the only three fields that change
silently.

### The competitor list starts empty — and that is not a bug here

There is no competitor list anywhere today:

* `competitor_masters`: **0 rows**;
* `opportunity_competitors`: **0 rows**;
* `/competitors` redirects to `/companies?relationship=Competitor`,
  and **`Company` has no `relationship` column**, so that filter can
  never match anything.

`CompetitorMaster` is the home chosen — it has a model, a unique
name, an is-active flag and a create API that folds duplicates. The
list fills from "+ Add a competitor…", and a name that already exists
(case- and space-insensitively) returns the existing row rather than
making a second.

---

## The Competitors page

`/competitors` was a redirect to `/companies?relationship=Competitor`.
`Company` has no `relationship` column, so that filter matched nothing
and the page showed an unfiltered company list. It is a real page now:

| Competitor | Deals lost to them | Last seen | Country |

Counted from `leads.lost_to_competitor_id`, which the Lost dialog
writes — not from `opportunity_competitors`, which has no rows and no
writer. Worst offender first.

**Add, rename and merge are admin only** (`admin.master`). Naming a
competitor while losing a deal is not: that goes through
`lost_capture.resolve_competitor`, not the admin endpoint, because
anybody closing a deal may say who took it.

**Merge** exists because the same company arrives spelled three ways
— "K+N", "Kuehne Nagel", "Kuehne + Nagel" — the list fills from a text
box at the moment somebody is losing a deal and is not thinking about
spelling. Merging **moves the leads first and removes the duplicate
second**, so no lead is ever left pointing at a competitor that no
longer exists, and the old spelling is kept as an alias on the
survivor so the same duplicate is not recreated by the next person who
types it. Audited.

### Retiring the old tasks

```
.venv/bin/python scripts/retire_lost_tasks.py --check
.venv/bin/python scripts/retire_lost_tasks.py --apply
```

One transaction: deactivates both definitions so no more are
generated, and closes the ~2,312 open instances as `Cancelled` with
the reason in each task's notes and an audit row each.

---

## Fault 1 — a Done button

```
GET  /api/tasks?lead=<id>     the tasks on a record
POST /api/tasks/<id>/done     finish one, with an optional note
```

On every task row in the Workbench list, and on the lead page in a
new "Tasks on this lead" panel.

* **Completed**, not Cancelled — a person pressing Done did the work.
  The bulk scripts cancel, because nobody did those.
* The note is **optional**. Demanding one every time turns a Done
  button into a Done-later button.
* Pressing Done twice is **not an error**: two people on the same row
  is ordinary, and the second should see the same outcome as the
  first. The second press writes no second audit row.
* Audit row per close, naming who and the note.

**Who may.** The owner; anyone whose data scope reaches the owner;
anyone with `admin.triage`. A role-owned task — escalated to
Vertical_Head with no named person — may be closed by anyone whose
scope is wider than their own leads, because otherwise it belongs to
everybody and so to nobody. The rule is `app/tasks/routes.may_close`,
named and tested directly rather than inferred from HTTP codes.

---

## Fault 2 — the lead form reaches the engine

`PUT /api/leads/<id>` now calls `_fire_task_hook`, **only when the
stage actually changes**. A corrected phone number is not a state
transition, and firing on every save would re-raise the same task on
every edit. Tested in both directions, including a save that
resubmits the stage it already had.

This replaces the narrower autoclose-only wiring added earlier the
same day: closing tasks on a closed lead now happens through the hook
like everything else, rather than beside it.

---

## Deploy

```
cd /var/www/procam-crm
git pull
.venv/bin/python scripts/2026_10_19_lost_capture.py --check
.venv/bin/python scripts/2026_10_19_lost_capture.py
.venv/bin/python scripts/2026_10_19_lost_capture.py --check
sudo systemctl restart procam-crm
.venv/bin/python scripts/retire_lost_tasks.py --check
```

The last one writes nothing. Stop there.
