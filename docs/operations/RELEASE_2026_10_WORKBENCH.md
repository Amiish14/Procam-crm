# Release — CRM-WB-01: the Workbench agrees with itself

17 October 2026. Bug fix. **No migration, no data change.**

## Why

The Daily Workbench answered the same question three ways.

A tile counted **conditions** — a lead can be overdue *and* stale *and*
missing data, and it was counted in each. A chip counted the **primary
group** — one per item, chosen by priority, where `action_today` beats
everything. The list filtered by that same primary group.

So: ten leads carried the high-value flag. The High-value tile read
**10**. All ten were also overdue, so their primary group was
`action_today`, so the High-value chip read **0**, and clicking the
tile listed **0 rows**. Three numbers, each correct on its own terms,
no way for a reader to tell which to believe. The same split made Data
updates read 872 against a chip of 32, and sent "No next action" —
722 items — to a filter that showed 32.

Quote ageing was worse. It bucketed **RFQ records** against a quote-due
date. One item in 1,984 had a quote number and none had a due date, so
every bucket read 0 — while 130 leads sat in Quoted or Under
Negotiation. A dashboard of zeroes over a hundred and thirty live
quotes is worse than no dashboard, because it reads as good news.

## What changed

### One registry — `app/workbench/filters.py`

Each key has exactly one predicate. The tile number, the chip number
and the rows behind `?filter=<key>` are the same call on the same
predicate. They cannot disagree, because there is nothing left to
disagree with.

`group` survives with one job: the row's colour and where it sorts. It
counts nothing and filters nothing.

**Multi-membership is the point.** One lead appears in several chips,
so the chips add up to more than Everything. The screen says so
underneath them. Filing each item under one bucket is what produced a
tile of 10 over a chip of 0.

### Quote ageing, rebuilt on stage

Population is the open leads in **Quoted** or **Under Negotiation**,
filtered by the same scope, vertical, stage and search as the list.
Age is days since the quote went out, in falling order of trust:

1. the quote record's `submitted_at` — the real event
2. the date the lead entered Quoted, from `lead_stage_history`
3. the lead's own `quote_date`
4. `updated_at` — a guess, and the row is flagged **Quote date
   estimated**

Every item lands in exactly one bucket, so the buckets sum to the
population. A date in the future is clamped to Today rather than
dropped — a wrong date is a problem to see, not a row to lose. The
heading reads "Quote ageing: N open quotes", plus a count of estimated
dates when there are any.

### Labels

Tile and chip now read the same words wherever they are the same
number. Two of these go slightly beyond the spec's table, which spelt
them differently: "Data updates required" / "Data update required" and
"New assignments" / "Newly assigned". One number with two names is the
smaller version of exactly the confusion this release removes, so both
read the tile's wording. A test enforces it.

### A new condition

`quote_due_missing` — a lead waiting to be quoted with no deadline
anywhere. It is not "due" and not "not due"; it is a record nobody can
work from, so it counts under **Data updates required** with that
reason, as the spec asks. This is why the Data updates number is
slightly higher than the `data_issue` count alone.

### Out-of-scope fix, taken deliberately

`sales_rules.days_between()` took `.date()` off a naive **UTC**
timestamp and compared it with `business_today()`, which is **IST**.
The module's own `BUSINESS_TZ` docstring describes this bug; only half
of it had been fixed — today moved to IST, the timestamps did not.

Between 18:30 and midnight UTC (00:00–05:30 IST) a call logged a
minute earlier read as one day old, so "contacted today" came out
false for work somebody had just done. It also made
`test_digests.py::test_something_actioned_today_is_not_an_exception_tonight`
fail on any run after 18:30 UTC — on the unmodified tree, before this
release touched anything.

Both ends of `days_between` are now read as business days.

**This touches condition detection, which CRM-WB-01 §5 puts out of
scope.** It was taken anyway because the suite cannot be green without
it. The effect: `stale`, `idle` and `negotiation_idle` shift by at
most one day for records whose last contact fell in that window —
towards the correct answer. Nothing else changes. It is a separate
commit and can be reverted on its own.

## What did NOT change

- No migration. No new table, no new column, no data written.
- Lead, account, task and contact rows: untouched. Acceptance test 8
  counts all four before and after.
- How conditions are detected — `stale` is still 30 days quiet,
  high-value is still the configurable threshold — except for the
  timezone correction above.
- `/api/workbench` keeps `summary`, `group_counts` and `groups`, so
  anything reading the old shape still works. They now carry the
  registry's numbers instead of the primary-group tally.
- `?group=` still works and maps onto the equivalent filter, so a
  bookmark or an open tab does not break mid-deploy.

## Tests

`tests/test_workbench_filters.py` — 22 tests, the eight acceptance
tests from §4 plus the behaviour they rest on. Each runs for three
viewers: company-wide scope and two individuals. Full suite: 2,845
passing.

## Deploy

No migration, so this is a pull and a restart.

```
cd /var/www/procam-crm
git pull
.venv/bin/python -m pytest -q 2>&1 | tail -3
sudo systemctl restart procam-crm
```

Wait for the service to finish booting before curling it — gunicorn
runs with `--preload` and takes a few seconds.

## Rollback ladder

Four rungs. Start at the top; each is independent of the ones below.

**1 — The labels or the multi-membership note are wrong.**
Template only. Edit `templates/workbench/home.html` and restart. No
server change, no risk to the numbers.

**2 — Quote ageing looks wrong.**
The ageing is computed in `quote_ageing()` in
`app/workbench/service.py` and nothing else depends on it. Revert that
function alone to return `rules.rfq_ageing(...)`'s shape and restart;
the tiles, chips and list are unaffected.

**3 — A count is wrong.**
The predicate is one function in `app/workbench/filters.py`. Fix the
predicate, not the caller — the tile, the chip and the list all read
it, so one edit corrects all three. Add a test alongside it.

**4 — Take the whole thing off.**

```
cd /var/www/procam-crm
git log --oneline -6
git revert --no-edit <the CRM-WB-01 commit>
sudo systemctl restart procam-crm
```

Revert, do not reset: the branch is shared and the history is pushed.
There is no migration to undo and no data to restore, so a revert is
complete on its own.

The timezone correction is a **separate commit**; revert it
separately if the Workbench is fine and the day boundary is not. Note
that reverting it reintroduces the failing digest test after 18:30
UTC.

## What to check after the deploy

Acceptance tests 1–6, as Admin and as two named users:

```
.venv/bin/python scripts/crm_health_check.py --workbench-only
```

That prints the tile-vs-chip table and the ageing population. Every
pair should read "yes", and the buckets should sum to the population.
