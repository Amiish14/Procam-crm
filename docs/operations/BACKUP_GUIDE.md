# Backup Guide

The CRM is one SQLite file. A good backup is a consistent copy of it that
has been opened and checked, kept somewhere the VM's own failure cannot
take with it.

## Take a backup

```bash
cd /var/www/procam-crm
.venv/bin/python scripts/backup_database.py --label <why>
```

Output ends with `check    ok`. Anything else means the backup is not
usable — take another and investigate before doing whatever needed it.

`--label` goes into the file name: `pre-deploy`, `pre-recovery`,
`pre-cleanup`, `daily`.

### Why not `cp`

`cp procam_crm.db backups/…` while gunicorn is writing can copy half a
transaction. The script uses SQLite's online backup API, which takes a
consistent snapshot while the CRM keeps running, then runs
`PRAGMA integrity_check` on the copy. A test proves an in-flight write
does not leak into the copy.

If `sqlite3` is installed, this is equivalent:
```bash
sqlite3 procam_crm.db ".backup backups/procam_crm-$(date +%F-%H%M%S)-manual.db"
sqlite3 backups/procam_crm-<stamp>-manual.db "PRAGMA integrity_check"
```

## When a backup is mandatory

| Before | Label |
|---|---|
| Every deploy | `pre-deploy` |
| Every migration script run outside a deploy | `pre-migration` |
| Email recovery (`--ids`, `--all`) | `pre-recovery` |
| Any script with `--apply`, bulk admin delete, restore scripts | `pre-cleanup` |
| Every day (scheduled, below) | `daily` |

## Schedule a daily backup

A systemd timer, installed once by someone with sudo. The unit files are
in [deploy/](deploy/).

```bash
sudo cp docs/operations/deploy/procam-crm-backup.service /etc/systemd/system/
sudo cp docs/operations/deploy/procam-crm-backup.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now procam-crm-backup.timer
systemctl list-timers | grep procam-crm-backup
```

It runs at 01:30 and keeps the newest 14 backups made by the script
(`--keep 14`). Pruning only ever removes `procam_crm-*.db` files; hand-made
backups with other names are never touched.

## Keep a copy off the VM

A backup on the same disk does not survive the disk. At least weekly,
copy the newest backup off the VM — to Azure Blob Storage, or to a
machine in the office:

```bash
# from the Mac
scp procam-app:/var/www/procam-crm/backups/procam_crm-<stamp>-daily.db \
    ~/ProcamBackups/
```

The file holds every customer, contact and email. Store it encrypted
(an encrypted disk image, or a storage account with encryption and
restricted access), never in email, chat or a shared drive.

Azure alternative: enable **Azure Backup** for the VM (whole-disk
snapshots, retained by Azure). It complements, not replaces, the
file-level backups, which are what you restore from in practice.

## Check backups are happening

```bash
ls -lt backups/ | head -5
.venv/bin/python scripts/production_preflight.py | grep -A0 backups
```

The preflight warns when the newest backup is older than 26 hours or
under half the live database's size.

## Test a restore (monthly)

A backup that has never been restored is a hope. Restore into a scratch
location and open it — this never touches the live database:

```bash
cp backups/procam_crm-<stamp>-daily.db /tmp/restore-test.db
.venv/bin/python - <<'EOF'
import sqlite3
db = sqlite3.connect('file:/tmp/restore-test.db?mode=ro', uri=True)
print(db.execute('PRAGMA integrity_check').fetchone()[0])
for t in ('leads', 'lead_emails', 'lead_notes', 'companies', 'employees'):
    print(t, db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0])
EOF
rm /tmp/restore-test.db
```

Compare the counts with the live system (they should be close to the
day's figures). For a full restore, see the
[Disaster Recovery Guide](DISASTER_RECOVERY_GUIDE.md).

## What is not in the database backup

| Item | Where | How to protect it |
|---|---|---|
| Email attachments | `EMAIL_INGEST_STORAGE_ROOT` (default `/var/www/procam-crm/uploads/email_leads`) | Include in the weekly off-VM copy: `tar czf attachments-<date>.tgz uploads/` |
| Other uploads (RFQ files, business cards) | `CRM_UPLOAD_ROOT` (default `instance/uploads`) | Same |
| `.env` (secrets) | `/var/www/procam-crm/.env` | Keep a copy in the company password manager, not with the backups |
| Graph subscription id | `.leads_subscription_id` | Not needed — `subscribe_leads_mailbox.py --enforce` recreates it |
| Recovery preimages | `backups/recovery_preimage_*.json` | Kept with the backups; needed to undo a recovery run |
