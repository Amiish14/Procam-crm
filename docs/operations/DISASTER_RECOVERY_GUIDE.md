# Disaster Recovery Guide

For when the database is corrupt or gone, or the VM itself is lost.

## Targets

| | Target | What sets it |
|---|---|---|
| RPO (data you can lose) | ≤ 24 hours | the daily backup timer; less if backups are more frequent |
| RTO (time to be back) | ≤ 2 hours on the same VM, ≤ 1 day on a new one | how practised the restore is |

These targets hold only once the daily backup timer and the weekly
off-VM copy (Backup Guide) are actually in place. Until then, RPO is
"since the last manual backup" and a lost VM may mean lost data.

---

## Scenario 1 — database corrupt, VM fine

Signs: `/healthz` returns 503; `journalctl -u procam-crm` shows
`database disk image is malformed`; preflight `integrity` FAILs.

```bash
cd /var/www/procam-crm
sudo systemctl stop procam-crm

# 1. keep the damaged file — do not overwrite it
mv procam_crm.db procam_crm.db.corrupt-$(date +%F-%H%M)
mv procam_crm.db-journal procam_crm.db-journal.corrupt 2>/dev/null || true

# 2. newest backup that passes its check
ls -lt backups/procam_crm-*.db | head
.venv/bin/python -c "import sqlite3,sys; d=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True); print(d.execute('PRAGMA integrity_check').fetchone()[0])" backups/<newest>.db

# 3. restore it
cp backups/<newest>.db procam_crm.db
chmod 600 procam_crm.db
sudo chown procamapp:procamapp procam_crm.db     # the service user

sudo systemctl start procam-crm
curl -s http://127.0.0.1:8002/healthz
.venv/bin/python scripts/production_preflight.py
```

Then recover what happened after the backup:

- **Emails**: re-ingest from the mailbox for the gap. Dry run first.
  ```bash
  .venv/bin/python scripts/2026_09_02_poll_leads_mailbox.py --hours <hours since backup> --dry-run
  .venv/bin/python scripts/2026_09_02_poll_leads_mailbox.py --hours <hours since backup>
  ```
  Ingest de-duplicates on the message id, so re-running is safe.
- **Manual work** (notes, stage changes, assignments made in the gap):
  cannot be recovered from the database. Tell the team the time of the
  backup so they can re-enter the day's changes.
- A copy of the corrupt file may still yield data:
  `sqlite3 procam_crm.db.corrupt-… ".recover" > recovered.sql` — have
  someone experienced review before loading anything from it.

## Scenario 2 — VM lost

1. **New VM** (Ubuntu, same region), user `procamapp`, Python 3.12, nginx.
2. **Code**
   ```bash
   sudo mkdir -p /var/www/procam-crm && sudo chown procamapp:procamapp /var/www/procam-crm
   cd /var/www/procam-crm
   git clone <repository-url> .
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
3. **Secrets**: recreate `.env` from the company password manager
   (never from chat or email). `chmod 600 .env`.
   Compare against `.env.example` for anything missing.
4. **Data**: copy the newest off-VM backup to
   `/var/www/procam-crm/procam_crm.db`, run the integrity check, `chmod 600`.
   Restore attachments: `tar xzf attachments-<date>.tgz`.
5. **Service**: recreate `/etc/systemd/system/procam-crm.service`
   (DEPLOY.md, Option B §3, with `--bind 127.0.0.1:8002`), enable, start.
6. **nginx**: the `/CRM/` block lives in the TMS repository
   (`hub/nginx-procamlogitech.conf`). Install, `nginx -t`, reload.
   TLS certificate: reissue with certbot for `procamlogitech.com`.
7. **Timers and log rotation**: install from `docs/operations/deploy/`.
8. **Graph**: the app registration is unaffected by a VM loss. Recreate
   the mailbox subscription (the webhook URL is unchanged if DNS points
   at the new VM):
   ```bash
   .venv/bin/python scripts/subscribe_leads_mailbox.py --enforce
   ```
   Then back-fill the gap with `2026_09_02_poll_leads_mailbox.py --hours …`.
9. **Verify**: `production_preflight.py` (no FAIL), smoke test, and have
   two users log in.

## Scenario 3 — secrets exposed

| Secret | Rotate by | Effect |
|---|---|---|
| `SECRET_KEY` | new value in `.env`, restart | everyone is logged out; nothing else |
| `MS_CLIENT_SECRET` | Entra admin center → the app → Certificates & secrets → new secret, update `.env`, restart, delete the old secret | none if done in that order |
| `EMAIL_WEBHOOK_SECRET` | new value in `.env`, restart, `subscribe_leads_mailbox.py --enforce` | notifications in the few minutes between restart and re-subscribe are refused; back-fill with the poll script |
| `GROQ_API_KEY` / `ANTHROPIC_API_KEY` | provider console → revoke, new key in `.env`, restart | none |
| A user's password | Employees → reset (issues a temporary password) | that user re-sets theirs |
| PCM001 / admin passwords | `scripts/audit_default_passwords.py --list`, then reset each | as above |

## Practise

Twice a year, restore the newest backup onto a scratch VM (or into
`/tmp` per the Backup Guide) and time it. The measured time is the real
RTO.
