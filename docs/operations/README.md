# Procam CRM — Operations

For the people who deploy, run and support the CRM on the production VM.

| Guide | Use it when |
|---|---|
| [Production Runbook](PRODUCTION_RUNBOOK.md) | Daily and weekly checks; the first page to open |
| [Monitoring Guide](MONITORING_GUIDE.md) | What /CRM/admin/ops and `ops_status.py` check, thresholds, what to do on WARN/FAIL, wiring alerts |
| [Deployment Guide](DEPLOYMENT_GUIDE.md) | Shipping a new version to the server |
| [Backup Guide](BACKUP_GUIDE.md) | Taking, checking and keeping backups |
| [Rollback Guide](ROLLBACK_GUIDE.md) | A deploy went wrong |
| [Disaster Recovery Guide](DISASTER_RECOVERY_GUIDE.md) | The database or the VM is lost or corrupt |
| [Troubleshooting Guide](TROUBLESHOOTING_GUIDE.md) | Something specific is broken |
| [Administrator Guide](ADMINISTRATOR_GUIDE.md) | Accounts, access, data quality, audits |
| [Data Quality Guide](DATA_QUALITY_GUIDE.md) | The Data Quality checks, batch correction and the daily trend snapshot |
| [Graph Setup Guide](GRAPH_SETUP_GUIDE.md) | Microsoft 365 permissions for the leads mailbox (for IT) |
| [AI Configuration Guide](AI_CONFIGURATION_GUIDE.md) | Procam AI Copilot, intake AI, external providers |
| [Production Readiness Report](PRODUCTION_READINESS_REPORT.md) | Sign-off: what is done, what is open, risks |

## Facts every guide assumes

| | |
|---|---|
| Host | Azure VM, reached as `procam-app` |
| Code | `/var/www/procam-crm` — **git pull only**; never commit or push from the server |
| Service | `procam-crm.service` (systemd) — gunicorn, 2 sync workers, `127.0.0.1:8002` |
| Public URL | `https://procamlogitech.com/CRM/` (nginx proxies `/CRM/` to 8002) |
| Python | `/var/www/procam-crm/.venv/bin/python` — the system `python3` has no Flask |
| Database | SQLite, `/var/www/procam-crm/procam_crm.db` (also `DATABASE_URL` in `.env`) |
| Backups | `/var/www/procam-crm/backups/` |
| Config | `/var/www/procam-crm/.env` — secrets live here and nowhere else |
| Logs | `journalctl -u procam-crm`; gunicorn files under `/var/log/procam-crm/` if the unit sets them |
| Health | `curl -s http://127.0.0.1:8002/healthz` → `{"ok": true}` |

The systemd unit's description line says 8001. It is wrong; trust
`systemctl show procam-crm -p ExecStart`.

All commands below are run from `/var/www/procam-crm` unless stated.
