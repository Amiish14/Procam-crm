# Monitoring Guide

What the CRM watches about itself, what each result means, and what to
do when one is not OK.

There are three pieces:

| Piece | Where | Runs |
|---|---|---|
| `scripts/ops_status.py` | the VM | every 10 min from `procam-crm-ops-status.timer`, or by hand |
| **/CRM/admin/ops** (JSON: `/CRM/api/ops/status`) | the browser, `admin.access` only | shows the timer's last report, plus a few live checks |
| `/CRM/healthz` | anyone, no login | for an external uptime monitor |

Every check is read-only. The database and backups are opened with
SQLite `mode=ro`; `git` runs with `--no-optional-locks`; `systemctl` is
only asked to `show` and `list`. The only calls off the VM are a
Microsoft Graph token, one Graph `GET /subscriptions`, and a TLS
handshake with the public CRM host — each with a timeout of a few
seconds, and all skipped by `--no-network`. No secret value is ever
printed.

## Statuses

| Status | Meaning | Who acts |
|---|---|---|
| **OK** | Measured, within limits | nobody |
| **WARN** | Measured, heading for trouble or needs a look this week | CRM administrator, same day |
| **FAIL** | Broken now, or will be within days | CRM administrator, now |
| **UNKNOWN** | Could not be measured here (tool missing, no permission, not configured, network skipped) | read the detail; fix the gap if the check matters |

UNKNOWN is never silently OK. A permanent UNKNOWN on a check you rely on
is a monitoring gap — fix its cause.

## Running it by hand

```bash
cd /var/www/procam-crm
.venv/bin/python scripts/ops_status.py                     # everything, table
.venv/bin/python scripts/ops_status.py --json              # machine-readable
.venv/bin/python scripts/ops_status.py --only backups,restore,disk
.venv/bin/python scripts/ops_status.py --no-network        # no Graph, no TLS
.venv/bin/python scripts/ops_status.py --no-schema         # skip the slow schema comparison
```

| Exit status | Meaning |
|---|---|
| 0 | nothing FAILed (OK, WARN and UNKNOWN only) |
| 1 | at least one FAIL |
| 2 | bad arguments (for example an unknown `--only` key) |
| 3 | the monitor itself broke (for example the status file could not be written) |

`--write-status PATH` also writes the JSON report, atomically. The timer
writes `/var/www/procam-crm/instance/ops_status.json`; the page reads the
path in `OPS_STATUS_FILE` (default `instance/ops_status.json` under the
code directory), so change both together if you move it.

## The admin page

/CRM/admin/ops shows:

- **Latest full report** — the timer's last run, with its age. Older than
  30 minutes is flagged: the timer has stopped
  (`systemctl status procam-crm-ops-status.timer`,
  `journalctl -u procam-crm-ops-status -n 30`).
- **Live from this page load** — database size and journal mode, the email
  and review queues, and Copilot index staleness. These are cheap,
  read-only queries; the web process never runs `quick_check`, `ps`,
  `systemctl`, `git` or a network call.

## The checks

Thresholds are constants at the top of `app/ops/checks.py`.

### health — Production health

`GET http://127.0.0.1:8002/healthz` (override with `OPS_HEALTH_URL`).

| Result | When |
|---|---|
| OK | HTTP 200, `{"ok": true}`, under 2 s |
| WARN | healthy but slower than 2 s — workers busy or VM loaded |
| FAIL | no answer, or 503 (the app is up but its database query failed) |

FAIL: `systemctl status procam-crm`, `journalctl -u procam-crm -n 100`,
then Troubleshooting Guide → "The CRM does not load".

### workers — gunicorn workers

The gunicorn master and its workers, from `ps` (or `/proc` when `ps` is
missing). The expected count is read from the unit's `ExecStart`
(`--workers N`), else from the master's command line.

| Result | When |
|---|---|
| OK | master present, worker count as configured |
| WARN | fewer workers than configured (they are crashing or being OOM-killed), or a worker above 1024 MB |
| FAIL | no master (service down) or a master with no workers |
| UNKNOWN | neither `ps` nor `/proc` readable |

WARN/FAIL: `journalctl -u procam-crm --since "1 hour ago" | grep -iE "worker|killed|memory"`;
`dmesg | grep -i oom` (needs sudo). A worker that keeps growing is a
memory leak — restart in a quiet moment and raise a ticket.

### database — Database integrity

`PRAGMA quick_check` on the live file, journal mode, size, WAL size, and
growth per day compared with the newest backup.

| Result | When |
|---|---|
| OK | quick_check `ok` |
| WARN | growth over 250 MB/day, or a WAL file over 256 MB |
| FAIL | quick_check reports anything else, or the file is missing |

FAIL is serious: take a backup of the current state immediately
(`scripts/backup_database.py --label pre-recovery`) and follow the
Disaster Recovery Guide, scenario 1. Unusual growth: look for an import
or a runaway job in the journal.

### disk — Disk space

Free space on the database volume and the backups volume (reported once
if they are the same volume).

| Result | When |
|---|---|
| OK | at least 5 GB free, under 85 % used, room for 3 copies of the database |
| WARN | under 5 GB free, 85 % used or more, or less than 3× the database size free |
| FAIL | under 1 GB, 95 % used or more, or the backups volume cannot hold one more backup |

Free space: prune old backups (`scripts/backup_database.py --keep 14`
after copying them off the VM), rotate logs (`logrotate-procam-crm`),
`journalctl --vacuum-time=30d` (sudo).

### backups — Newest backup

The newest file in `backups/`.

| Result | When |
|---|---|
| OK | under 26 h old and at least half the live size |
| WARN | 26–72 h old (last night's backup is missing), or under half the live size |
| FAIL | no backup, an empty file, or older than 72 h |

WARN/FAIL: `journalctl -u procam-crm-backup -n 20`; run
`.venv/bin/python scripts/backup_database.py --label manual` now. Backup
Guide.

### restore — Restore verification

Opens the newest backup read-only, runs `quick_check`, and compares row
counts of the core tables (employees, leads, companies, contacts,
opportunities, lead_notes, lead_emails) with the live database.

| Result | When |
|---|---|
| OK | quick_check `ok`; counts within tolerance |
| WARN | live has fewer rows than the backup by more than 2 % (at least 5) — something was deleted since; or live is ahead by more than 25 % (at least 200) — the backup is stale |
| FAIL | no backup, the backup does not open, fails quick_check, or lacks a core table live has |

FAIL: take a fresh backup and check it passes; do not rely on the failed
file. WARN "fewer rows": check `/CRM/admin/audit` for deletions and
purges since the backup time.

### rollback — Rollback readiness

Is there a `*-pre-deploy*` backup taken after the running commit was
made (`git log -1 --format=%ct HEAD`), and does it pass quick_check?

| Result | When |
|---|---|
| OK | yes, and it opens cleanly |
| WARN | no pre-deploy backup, or the newest predates the running commit — this deploy was made without one |
| FAIL | the pre-deploy backup fails quick_check |
| UNKNOWN | git cannot read the checkout |

WARN: take one now (`--label pre-deploy`) so a database rollback does
not lose a whole day; Deployment Guide step 2 is the habit to keep.

### deployment — Deployed code

Current commit and branch, the last time `HEAD` moved and how (git
reflog — the deploy marker), whether tracked files were changed on the
server, and whether the service was restarted after the code changed.

| Result | When |
|---|---|
| OK | clean tree, service started after the last checkout |
| WARN | tracked files modified on the server (it is pull-only), or the code changed after the service started (restart pending — old code is serving) |
| UNKNOWN | git cannot read the checkout (not a checkout, git missing, or a "dubious ownership" refusal for this user) |

A detached HEAD is normal during a code rollback (Rollback Guide §A); the
branch is shown, not judged. Modified files: find out who and why before
the next `git pull --ff-only` refuses (Troubleshooting → "git pull
refused"). Restart pending: `sudo systemctl restart procam-crm` when
intended.

### schema — Schema drift

Production preflight's schema comparison: the tables, columns and
indexes the code declares versus the live database. The expected schema
is built by importing the app in a child process against an in-memory
database — the live file is only opened read-only. Takes a few seconds;
`--no-schema` skips it.

| Result | When |
|---|---|
| OK | nothing missing |
| WARN | missing tables or indexes (a migration not run; not an outage) |
| FAIL | missing columns — pages using that table fail |

FAIL: run the missing migration (Deployment Guide step 4) or roll the
code back (Rollback Guide §A).

### config — Configuration

Production preflight's configuration checks: secrets present and not
published placeholders, secure cookies, DEBUG off, `URL_PREFIX`, AI hosts
private, `.env` permissions. Values are never shown.

FAIL/WARN: the detail names the setting; the Production Readiness Report
lists the known WARNs. Fix in `.env`, then restart.

### timers — Scheduled tasks

`systemctl list-timers --all` for `procam-crm-*` timers; for each, the
last and next run (`LastTriggerUSec`, `NextElapseUSecRealtime`) and the
result of the service it starts.

| Timer | Normal gap |
|---|---|
| procam-crm-backup | 26 h |
| procam-crm-copilot-index | 26 h |
| procam-crm-graph-subscription | 13 h |
| procam-crm-sla-sweep | 30 min |
| procam-crm-ops-status | 30 min |

| Result | When |
|---|---|
| OK | every expected timer installed, ran within its gap, last result `success` |
| WARN | a timer not installed, never run, or overdue |
| FAIL | a job's last run did not succeed |
| UNKNOWN | `systemctl` missing or not usable (not a systemd host) |

FAIL: `journalctl -u procam-crm-<job>.service -n 50`, fix, then
`sudo systemctl start procam-crm-<job>.service`. Not installed:
[deploy/README.md](deploy/README.md).

### email_queue — Email ingest queue

`email_events` in the last 24 h by status, plus events left at
`received`/`processing` in the last 7 days (the webhook stopped part way
through a message — a lead that may never be created).

| Result | When |
|---|---|
| OK | events arriving, no failures, nothing unprocessed for 30 min |
| WARN | any `failed` or `rejected` events; an unprocessed event 30 min–6 h old; or no events at all in 24 h |
| FAIL | an unprocessed event older than 6 h |

`failed`: read the reasons on the Email Inbox admin page and retry.
`rejected`: notifications for a different mailbox — a stray subscription
exists; run the graph-subscription service (`--enforce` removes it). No
events: check `graph_subscription` and Troubleshooting → "New email
enquiries are not arriving".

### review_queue — Lead review queue

`email_classifications` waiting for a human (`review_state = 'pending'`).

| Result | When |
|---|---|
| OK | empty, or a handful under two days old |
| WARN | more than 50 waiting, or the oldest waiting more than 48 h |

WARN: assign a reviewer (/CRM/lead-review). A sudden jump usually means
the classifier or the mailbox changed.

### graph_subscription — Graph mailbox subscription

With credentials and network: Graph `GET /subscriptions`, matched to the
id in `.leads_subscription_id`. Otherwise (or when Graph refuses): an
estimate from the id file's age — the file is rewritten on every create
and renew, and a subscription lives at most 70.5 h.

| Result | When |
|---|---|
| OK | expires in 24 h or more |
| WARN | expires within 24 h; the id file names a subscription Graph does not list; or there is no id file |
| FAIL | expired, or Graph lists no subscription at all |

WARN/FAIL: `sudo systemctl start procam-crm-graph-subscription.service`,
then `journalctl -u procam-crm-graph-subscription -n 30`. If it fails,
see `graph_token`.

### graph_token — Graph token

Can a client-credentials token be issued with `MS_TENANT_ID`,
`MS_CLIENT_ID`, `MS_CLIENT_SECRET`? The token is used for the
subscription check and never shown.

| Result | When |
|---|---|
| OK | a token was issued |
| FAIL | Microsoft refused — the detail names the AADSTS code: `AADSTS7000222` secret expired, `AADSTS7000215` secret wrong, `AADSTS700016` app not found, `AADSTS90002` tenant not found |
| UNKNOWN | credentials not configured, `--no-network`, or login.microsoftonline.com did not answer |

FAIL for an expired or wrong secret: Graph Setup Guide §5 (new secret),
update `.env`, restart the service and the subscription job.

### graph_secret_expiry — Graph client secret expiry

The CRM app registration cannot read its own secret's expiry without an
extra directory permission it should not have. So this is **UNKNOWN**
until an administrator copies the date from Entra (App registrations →
the CRM app → Certificates & secrets) into `.env`:

```bash
GRAPH_CLIENT_SECRET_EXPIRES=2027-03-31
```

| Result | When |
|---|---|
| OK | 60 days or more left |
| WARN | under 60 days — schedule the rotation |
| FAIL | under 14 days, or expired |

Update the date every time the secret is rotated.

### tls_certificate — TLS certificate

A TLS handshake (with SNI) to the host in `CRM_BASE_URL`, reading the
certificate's expiry.

| Result | When |
|---|---|
| OK | 21 days or more left |
| WARN | under 21 days — automatic renewal has not run |
| FAIL | under 7 days, or the certificate is rejected (expired, wrong name, untrusted) |
| UNKNOWN | `CRM_BASE_URL` unset, `--no-network`, or the host is not reachable from the VM |

WARN/FAIL: `sudo certbot renew --dry-run`, then `sudo certbot renew` and
`sudo systemctl reload nginx` (or whatever issues the certificate on this
VM). An UNKNOWN "not reachable" can mean the VM cannot reach its own
public address — check from outside with the uptime monitor instead.

### copilot_index — Copilot index

Coverage (leads with chunks versus leads with an enquiry) and staleness
(leads updated, emails and notes added after the newest chunk).

| Result | When |
|---|---|
| OK | at least 80 % coverage; built within 26 h or nothing changed since |
| WARN | never built, under 80 % coverage, changes waiting and the newest chunk older than 26 h, or the table missing |

WARN: `journalctl -u procam-crm-copilot-index -n 20`; run
`.venv/bin/python scripts/build_copilot_index.py`.

## Wiring alerts

### External uptime monitor → /healthz

From outside the VM, so it still alerts when the VM or nginx is down:

- URL: `https://procamlogitech.com/CRM/healthz`
- Method `GET`, no login, every 1–5 minutes (the endpoint is
  rate-limited to 60 requests a minute; behind nginx that limit may be
  shared by every caller, so do not poll faster)
- Healthy: HTTP **200** and body contains `"ok":true`
- Alert after 2 consecutive failures; 503 means the app is up but the
  database query failed

It says nothing else by design; everything detailed is behind login.

### Cron alert → ops_status exit code

The timer keeps the page current; an alert needs someone to be told. As
the service user (`sudo crontab -u procamapp -e`):

```cron
# Every 15 minutes: alert on any FAIL (exit 1) or a broken monitor (exit 3).
*/15 * * * * cd /var/www/procam-crm && .venv/bin/python scripts/ops_status.py --no-schema > instance/ops-alert.txt 2>&1 || <your alert command> < instance/ops-alert.txt

# Every 30 minutes: alert if the timer has stopped refreshing the report.
*/30 * * * * test -n "$(find /var/www/procam-crm/instance/ops_status.json -mmin -30 2>/dev/null)" || echo "ops_status.json is stale" | <your alert command>
```

`<your alert command>` is whatever the team already uses — `mail -s
"Procam CRM FAIL" <team address>`, or a `curl` to a Teams/Slack incoming
webhook. Keep any webhook URL in the crontab or `.env` on the VM, never
in the repository.

WARN never pages. Review WARNs on the admin page in the daily check
(Production Runbook).

## Environment variables

| Variable | Default | Used by |
|---|---|---|
| `OPS_STATUS_FILE` | `instance/ops_status.json` under the code directory | the admin page (the timer passes `--write-status`) |
| `OPS_HEALTH_URL` | `http://127.0.0.1:8002/healthz` | `health` |
| `OPS_SERVICE_NAME` | `procam-crm` | `workers`, `deployment` |
| `GRAPH_CLIENT_SECRET_EXPIRES` | unset → UNKNOWN | `graph_secret_expiry` |
| `CRM_BASE_URL` | already set for notification links | `tls_certificate` |
| `LEADS_SUBSCRIPTION_ID_FILE` | `.leads_subscription_id` | `graph_subscription` |
