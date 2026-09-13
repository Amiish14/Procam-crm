# Production Runbook

What to check, how often, and what "normal" looks like. Commands run on
the VM from `/var/www/procam-crm`. Everything in the daily and weekly
checks is read-only.

## Daily (5 minutes, morning)

Start at **/CRM/admin/ops** (or `.venv/bin/python scripts/ops_status.py`):
most of the checks below in one report, refreshed every 10 minutes. What each
line means and what to do about it: [Monitoring Guide](MONITORING_GUIDE.md).

```bash
cd /var/www/procam-crm
systemctl is-active procam-crm                                   # active
curl -s http://127.0.0.1:8002/healthz                            # {"ok":true}
journalctl -u procam-crm --since "24 hours ago" --no-pager \
  | grep -cE "Traceback|autoheal FAILED|unhandled error"         # 0, or investigate
ls -lt backups/procam_crm-*.db | head -2                         # one from last night
systemctl list-timers --no-pager | grep procam                   # timers scheduled
```

| Look at | Normal | If not |
|---|---|---|
| /CRM/lead-review queue | a handful, cleared daily | assign someone; a big jump means the classifier or mailbox changed |
| New email leads yesterday | similar to the usual daily count | Troubleshooting → "New email enquiries are not arriving" |
| Journal errors | 0 Tracebacks | read them; recurring ones become tickets |

## Weekly (20 minutes, Monday)

```bash
.venv/bin/python scripts/production_preflight.py
.venv/bin/python scripts/email_recovery_report.py
.venv/bin/python scripts/audit_default_passwords.py
.venv/bin/python scripts/data_quality_report.py
df -h /var/www
```

- Preflight: **0 fail**. WARNs are known items (see the Production
  Readiness Report); a *new* WARN needs a look.
- Copy the newest backup and the attachments folder off the VM (Backup
  Guide).
- Data-quality CSVs to the sales review; owners work their rows.
- /CRM/intake-intelligence: accuracy trend, false positives, proposals
  waiting.
- /CRM/copilot-analytics: unanswered questions, "not helpful" reasons.

## Monthly

- Test-restore a backup (Backup Guide → "Test a restore").
- Graph client secret: expiry more than 60 days away (Graph Setup Guide §5).
- `sudo apt list --upgradable` on the VM; security updates in a maintenance window.
- `.venv/bin/pip list --outdated` — upgrades go through the Mac, tests,
  and a normal deploy, never `pip install -U` on the server.
- Review who has admin and All-scope access (User Access Matrix).
- Deactivate leavers (Employees) — deactivation ends their session at once.

## Quarterly

- Rotate `EMAIL_WEBHOOK_SECRET` (Disaster Recovery Guide, scenario 3).
- Re-run the performance benchmark against a copy of the newest backup
  on a spare port; compare with the baseline in the readiness report:
  ```bash
  cp backups/<newest>.db /tmp/perf.db
  .venv/bin/python scripts/perf_benchmark.py --db /tmp/perf.db --port 8099 --seconds 20
  rm /tmp/perf.db
  ```
  Run it out of hours; it loads the VM's CPU while it runs.

## Scheduled jobs (once installed)

| Timer | When | Check |
|---|---|---|
| procam-crm-backup | 01:30 daily | `journalctl -u procam-crm-backup -n 5` ends `check    ok` |
| procam-crm-copilot-index | 02:15 daily | `journalctl -u procam-crm-copilot-index -n 5` |
| procam-crm-graph-subscription | every 12 h | subscription expiry > 1 day (`subscribe_leads_mailbox.py` lists it) |
| procam-crm-sla-sweep | every 15 min | `journalctl -u procam-crm-sla-sweep -n 5` |
| procam-crm-ops-status | every 10 min | /CRM/admin/ops report age under 30 min |

Templates and install steps: [deploy/](deploy/).

## Incidents

1. Is it down for everyone? `/healthz` from the VM, then the public URL.
2. Troubleshooting Guide for the symptom.
3. If a deploy in the last 24 h is the likely cause → Rollback Guide §A.
4. Data loss or corruption → Disaster Recovery Guide.
5. Afterwards: what happened, when, impact, what fixed it, what stops it
   recurring.

## Change rules

- Code changes only through the Deployment Guide (Mac → push → server
  pull). The server is pull-only.
- Secrets only in `.env` (chmod 600) and the company password manager.
- A backup before any migration, recovery, cleanup or `--apply` script.
- Read the dry run before any script that writes.
- Turning on `PROCAM_AI_ACTIONS`, a model host, or changing public AI
  providers needs a named business owner's approval.
