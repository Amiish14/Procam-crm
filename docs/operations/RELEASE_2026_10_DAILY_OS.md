# Release — the daily operating system (2026-10)

Everything in this release is additive. No existing table is altered, no
API is renamed, no URL moves except one: the task list that used to be
at `/CRM/my-work` is now at `/CRM/my-work/tasks`, because the Daily
Workbench took the main address.

---

## 1. What a person sees that they did not before

| Screen | Address | Who |
|---|---|---|
| Daily Workbench | `/CRM/my-work` | everyone |
| Team Workbench | `/CRM/team-workbench` | anyone whose data scope reaches more than themselves |
| Individual Sales Review | `/CRM/review/individual` | self; a manager may review the people their scope reaches |
| Vertical Sales Review | `/CRM/review/vertical` | needs a scope wider than "own records" |
| Review Meeting Mode | `/CRM/review/meeting` | as above; actions carry forward |
| Management Command View | `/CRM/management` | managers (refused to a viewer with only their own records) |
| Global CRM / contacts | `/CRM/global-crm` | everyone, scope-filtered |
| CRM health (hygiene score) | `/CRM/hygiene` | everyone, scope-filtered |
| Project intelligence | `/CRM/intelligence/projects` | everyone |
| Competitor intelligence | `/CRM/intelligence/competitors` | everyone |
| Vendor / vessel / port calls | `/CRM/intelligence/vendors` | everyone |
| Intelligence sources | `/CRM/admin/intelligence/sources` | `admin.master` |

---

## 2. Migrations — in this order

Each takes `--check` first (writes nothing) and is safe to re-run. The
boot also creates what it can, so a restart before a migration does not
break a page.

```bash
.venv/bin/python scripts/2026_10_06_workbench.py --check && \
.venv/bin/python scripts/2026_10_06_workbench.py
.venv/bin/python scripts/2026_10_07_review_actions.py --check && \
.venv/bin/python scripts/2026_10_07_review_actions.py
.venv/bin/python scripts/2026_10_08_contact_relationships.py --check && \
.venv/bin/python scripts/2026_10_08_contact_relationships.py
.venv/bin/python scripts/2026_10_09_intelligence.py --check && \
.venv/bin/python scripts/2026_10_09_intelligence.py
.venv/bin/python scripts/2026_10_10_escalation.py --check && \
.venv/bin/python scripts/2026_10_10_escalation.py
```

| Migration | Adds | Backfill |
|---|---|---|
| `2026_10_06_workbench` | `leads.next_action` | none |
| `2026_10_07_review_actions` | `review_actions` + 7 indexes | none |
| `2026_10_08_contact_relationships` | `contact_relationships`, `contact_assignments` | none — **no column is added to `contacts`**, deliberately: a model column the database lacks breaks every reader of that table |
| `2026_10_09_intelligence` | 9 intelligence tables, 6 Master Data lists, 55 vocabulary values | **no intelligence rows** — the registers ship empty |
| `2026_10_10_escalation` | `escalation_log`, the `escalation_rule` Master Data list | none |

Each has `--down` (with `--yes`) for rollback. `--down` on the
intelligence or review migrations drops the data captured since, which
is the point of taking the backup first.

---

## 3. Scheduled jobs

Already installed: backup, copilot-index, ops-status, dq-snapshot, plus
the pre-existing `procam-crm-poll`, `procam-crm-graph-renew` and
`procam-crm-sla`.

New in this release — install from `docs/operations/deploy/`:

| Timer | When (UTC / IST) | What |
|---|---|---|
| `procam-crm-daily-report` | 02:30 / 08:00, Mon–Sat | each person's action report |
| `procam-crm-exceptions-report` | 13:30 / 19:00, Mon–Sat | unresolved exceptions only |
| `procam-crm-weekly-report` | Mon 03:00 / 08:30 | weekly user, then vertical head |
| `procam-crm-monthly-report` | 1st 03:30 / 09:00 | management report |
| `procam-crm-escalation` | every 15 min | the RFQ/quote escalation ladder |

```bash
cd /var/www/procam-crm/docs/operations/deploy
for j in daily-report exceptions-report weekly-report monthly-report escalation; do
  sudo cp procam-crm-$j.service procam-crm-$j.timer /etc/systemd/system/
done
sudo systemctl daemon-reload
for j in daily-report exceptions-report weekly-report monthly-report escalation; do
  sudo systemctl enable --now procam-crm-$j.timer
done
```

**Before enabling the report timers**, prove one works and that nobody
is about to be mailed a backlog:

```bash
.venv/bin/python scripts/send_reports.py --daily --dry-run
.venv/bin/python scripts/send_reports.py --daily --to <YOUR-CODE>
.venv/bin/python scripts/escalation_sweep.py --dry-run
```

The escalation sweep has a backlog window (720 hours by default, in
Master Data) precisely so its first run does not mail everybody about
every historical RFQ.

---

## 4. Configuration

No new required variables. Everything optional:

| Setting | Where | Default |
|---|---|---|
| Escalation timings per level | Master Data → `escalation_rule` | received 0 h, owner reminder 24 h, approaching −24 h, overdue 0 h, head +24 h, next level +72 h |
| Escalation backlog window | Master Data → `escalation_rule` → `max_backlog` | 720 h |
| Intelligence source feeds and keys | Master Data / `public_sources`, plus the named environment variables per adapter | unset — the adapter reports why it cannot run |
| Report recipients | derived from active employees and their scope | — |
| `NOTIFY_ENABLED` | `.env` (existing) | on; off means in-app only |

Reminder and report email uses the existing Graph sender, so it needs
the same **Mail.Send** grant as assignment email. Without it, in-app
notifications still arrive and the email is reported as refused.

---

## 5. External dependencies — honest status

| Adapter | Status | Needs |
|---|---|---|
| Manual entry | **IMPLEMENTED & OPERATIONAL** | nothing |
| CRM news inbox | **IMPLEMENTED & OPERATIONAL** | nothing (reads bulletins already in the CRM) |
| RSS / Atom project feed | **IMPLEMENTED — EXTERNAL DATA SOURCE REQUIRED** | a feed URL; Indian project/tender feeds are commercial |
| JSON project API | **IMPLEMENTED — CREDENTIAL REQUIRED** | paid API subscription and key |
| AIS / port-call feed | **IMPLEMENTED — EXTERNAL DATA SOURCE REQUIRED** | AIS or port-community subscription licensed for CRM use |
| Vessel / carrier register | **IMPLEMENTED — EXTERNAL DATA SOURCE REQUIRED** | vessel register subscription |

An unavailable adapter returns nothing and records why. No sample or
seeded intelligence exists anywhere in the release.

---

## 6. Rollback

1. `sudo systemctl stop` the five new timers, and `disable` them.
2. `git checkout <previous commit>` (the deploy note records it) and
   `sudo systemctl restart procam-crm`. The new tables simply stop being
   read; nothing else refers to them.
3. Only if the schema itself must go back: run each migration's
   `--down --yes`, newest first. That discards review actions,
   intelligence captured, contact relationships and the escalation log.
4. The database rollback of last resort is the labelled pre-deploy
   backup, which loses everything entered since it was taken.

---

## 7. Known differences this release does not resolve

The CRM still defines some KPIs more than once — four rules for "open",
five for win rate, two for pipeline value, three ageing schemes, and the
KPI targets engine drops the vertical/user filter for `won_value` and
`pipeline_value`. Everything built here reads one module
(`app/services/sales_rules.py`), but changing the existing dashboards,
KPI engine, PIC 360 and Copilot would move numbers people already rely
on. See [Workbench](WORKBENCH.md) §7 for the list.
