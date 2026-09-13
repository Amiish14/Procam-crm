# Scheduled jobs and log rotation — templates

Nothing in this folder is installed automatically. Each file is a
template for someone with sudo on the VM.

Before installing, confirm the service user and paths match production:

```bash
systemctl show procam-crm -p User -p WorkingDirectory -p ExecStart
systemctl list-timers --all | grep -i procam      # what already exists
```

The templates assume `User=procamapp` and `/var/www/procam-crm`. Edit
them if `systemctl show` says otherwise. If a timer for the same job
already exists (a subscription renewal timer is referred to in
`scripts/subscribe_leads_mailbox.py`), keep one, not both.

| Files | Job | Schedule |
|---|---|---|
| `procam-crm-backup.{service,timer}` | Verified database backup, keep newest 14 | daily 01:30 |
| `procam-crm-graph-subscription.{service,timer}` | Renew (or recreate) the leads mailbox subscription | every 12 h |
| `procam-crm-copilot-index.{service,timer}` | Rebuild the Copilot search index | daily 02:15 |
| `procam-crm-sla-sweep.{service,timer}` | Task SLA reminders | every 15 min |
| `logrotate-procam-crm` | Rotate gunicorn logs in `/var/log/procam-crm/` | daily, 30 kept |

Install a pair:

```bash
sudo cp procam-crm-backup.service procam-crm-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now procam-crm-backup.timer
sudo systemctl start procam-crm-backup.service     # run once now
journalctl -u procam-crm-backup.service -n 20
```

Install log rotation:

```bash
sudo cp logrotate-procam-crm /etc/logrotate.d/procam-crm
sudo logrotate --debug /etc/logrotate.d/procam-crm   # dry run
```

Not scheduled on purpose: `scripts/intake_retention.py --apply` (trims
stored email bodies) and any deletion of `copilot_log` or `email_events`
rows. How long those are kept is a business decision; see the Production
Readiness Report.
