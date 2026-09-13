# Troubleshooting Guide

Start every investigation with the same three commands:

```bash
cd /var/www/procam-crm
systemctl status procam-crm --no-pager | head -15
journalctl -u procam-crm --since "30 min ago" --no-pager | tail -80
.venv/bin/python scripts/production_preflight.py
```

---

## The CRM does not load

| Check | Command | Meaning |
|---|---|---|
| Service | `systemctl is-active procam-crm` | `failed` → read the journal for the Traceback |
| App | `curl -s http://127.0.0.1:8002/healthz` | `{"ok":true}` → app fine, look at nginx; 503 → database; no answer → gunicorn not running |
| nginx | `sudo nginx -t && systemctl is-active nginx` | the `/CRM/` block proxies to 8002 |
| Port | `systemctl show procam-crm -p ExecStart` | must bind `127.0.0.1:8002` (the unit's description says 8001 — ignore it) |

**Boot fails with `RuntimeError: ADMIN_INITIAL_PASSWORD must be set`** —
the database has no admin at all (wrong `DATABASE_URL`, or an empty file).
Check `grep DATABASE_URL .env` points at `/var/www/procam-crm/procam_crm.db`
before setting anything. Do not "fix" it by seeding a new admin into the
wrong database.

**`OperationalError: no such column`** — schema drift. The boot adds
known columns automatically; if one is still missing, the preflight
names it. Run the migration that adds it (Deployment Guide §4), or roll
back the code (Rollback Guide §A).

**`database is locked`** in the journal — a long write (a migration or
script) holds the database. Wait for it, or find it:
`ps aux | grep scripts/`. Don't run two writing scripts at once.

**`database disk image is malformed`** — Disaster Recovery Guide,
scenario 1.

## Everyone is logged out / "CSRF token missing" / "The CSRF session token is missing"

- `SECRET_KEY` unset or changed. The boot log warns
  `SECRET_KEY is not set`. Set it in `.env` and restart (everyone logs in
  once more).
- Browser on `http://` instead of `https://` — the session cookie is
  Secure-only.

## A user cannot do anything after logging in

| Response | Cause | Fix |
|---|---|---|
| 403 `password_change_required` | still on a temporary password | log out, log in, set a new password |
| 401 `inactive` | account deactivated | reactivate in Employees if intended |
| 403 `Admin access required` / missing menu items | role or Access Matrix profile | check both |
| "not found" opening a record by link | the record is outside their scope (since 2026-09 by-id links respect scope) | expected; assign the record or widen the profile |

## New email enquiries are not arriving

Work down the chain:

```bash
# 1. is the subscription alive? (lapses after ~2.9 days without renewal)
.venv/bin/python scripts/subscribe_leads_mailbox.py          # lists subscriptions
ls -l .leads_subscription_id
systemctl list-timers | grep -i subscription

# 2. is Graph answering? (403 = permissions, see Graph Setup Guide)
.venv/bin/python scripts/2026_09_02_check_graph_permissions.py

# 3. are notifications reaching the app?
journalctl -u procam-crm --since "1 hour ago" | grep -i webhook

# 4. what did the classifier do with them?
#    /CRM/lead-review and /CRM/intake-intelligence
```

| Journal line | Cause | Fix |
|---|---|---|
| `EMAIL_WEBHOOK_SECRET is not set — refusing` | secret missing in `.env` | set it, restart, `subscribe_leads_mailbox.py --enforce` |
| `clientState mismatch` | secret changed after the subscription was created | `subscribe_leads_mailbox.py --enforce` |
| nothing at all | subscription expired or nginx not forwarding | renew; `curl -X POST "https://procamlogitech.com/CRM/api/email/webhook?validationToken=ping"` must echo `ping` |
| `403` from Graph | Mail.Read not granted / policy wrong | Graph Setup Guide §3–§4 |

Back-fill a gap once fixed (dedupes on message id):
```bash
.venv/bin/python scripts/2026_09_02_poll_leads_mailbox.py --hours 24 --dry-run
.venv/bin/python scripts/2026_09_02_poll_leads_mailbox.py --hours 24
```

## Assignment emails are not sent

`journalctl -u procam-crm | grep "notification send failed"` shows a 403 →
`Mail.Send` not granted (Graph Setup Guide). To stop the attempts meanwhile:
`NOTIFY_ENABLED=false` in `.env`, restart. Test when fixed:
`scripts/2026_09_02_test_notification.py --to <you>`.

## Copilot

| Symptom | Cause | Fix |
|---|---|---|
| Search finds nothing recent | index not rebuilt since the emails/notes arrived | `scripts/build_copilot_index.py`; install the nightly timer |
| "No model host configured" | `PROCAM_AI_BASE_URL` unset — expected, answers are still given | AI Configuration Guide |
| "…is a public AI API" | host refused by policy | use a Procam-hosted endpoint |
| Clicking a result chip 404s | pre-2026-09 panel links | deploy current code |
| Actions "switched off" | `PROCAM_AI_ACTIONS` is off (default) | only enable with approval |

## Academy self-check does nothing

Fixed in 2026-09 (CSRF header). If it recurs: browser console — a
`400` with `code: csrf` means the page lost its token; refresh. Any HTML
response to `/api/...` is a bug: every `/api` error is JSON.

## Email recovery

`email_recovery_report.py` says credentials not configured → the `MS_*`
variables are missing from `.env` (it used to check the wrong names; if
you are on older code, ignore it). 403 on `--check` → Mail.Read not
granted.

## Slow pages

- Check load: `uptime`, `top -o %CPU`. Two gunicorn workers serve about
  10 requests/second on the dashboard-heavy mix (benchmark in the
  Production Readiness Report). Many people on the dashboard at once
  queue behind each other.
- Heaviest endpoints: dashboard summary (loads every lead), Copilot
  "open leads", the 500-row lead list.
- Increasing workers is a config change — see the report's performance
  section; measure with `scripts/perf_benchmark.py --db <copy of a backup>`
  first.

## `git pull` refused on the server

`git status` shows local changes. The server is pull-only, so local
changes are unexpected: **look before discarding**.

```bash
git status
git diff --stat
```

If they are files the app writes (e.g. under `data/`), move them aside,
pull, and compare. Never `git reset --hard` without a backup of the
changed files; never commit on the server.

## Getting help

Collect before escalating: the preflight output, the last 200 journal
lines, the time it started, the commit (`git log --oneline -1`), and
what changed last.
