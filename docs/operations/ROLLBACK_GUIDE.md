# Rollback Guide

A deploy went wrong. Pick the smallest rollback that fixes it.

| Symptom | Rollback | Data lost |
|---|---|---|
| New code misbehaves, database fine | **A. Code only** | None |
| A migration or script wrote bad data | **B. Code + restore the pre-deploy backup** | Everything written since the backup |
| An email recovery run was wrong | **C. Undo the recovery run** | None |
| One migration's columns must go | **D. That migration's `--down`** (rarely right) | The column's contents |

Before any rollback, take a backup of the current state — it is the only
way back from a rollback that was the wrong call:

```bash
cd /var/www/procam-crm
.venv/bin/python scripts/backup_database.py --label pre-rollback
```

---

## A. Code only (the usual answer)

Every migration in this codebase adds nullable columns or new tables.
Older code ignores them, so the previous commit runs happily on the
newer schema.

```bash
cd /var/www/procam-crm
git log --oneline -5                       # find the last good commit
git checkout <good-hash>                   # detached HEAD, deliberately
sudo systemctl restart procam-crm
curl -s http://127.0.0.1:8002/healthz      # {"ok":true}
.venv/bin/python scripts/production_preflight.py
```

The preflight will WARN about tables/indexes the older code does not
declare — expected. It must not FAIL on columns.

To return to normal once the fix is pushed:

```bash
git checkout main
git pull --ff-only origin main
sudo systemctl restart procam-crm
```

Do not `git reset --hard` or commit on the server.

## B. Code + database restore

Only when data is wrong, not merely code. **Everything written after the
backup — new leads, notes, emails ingested, assignments — is lost.** Tell
the team first, and check the email ingest will re-fetch what it can
(`scripts/2026_09_02_poll_leads_mailbox.py --hours <gap> --dry-run`
after the restore shows what would come back).

```bash
cd /var/www/procam-crm
sudo systemctl stop procam-crm
.venv/bin/python scripts/backup_database.py --label pre-restore  # current state, just in case
ls -lt backups/ | head                                           # find the pre-deploy file

# check the backup before trusting it
.venv/bin/python -c "import sqlite3,sys; d=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True); print(d.execute('PRAGMA integrity_check').fetchone()[0])" backups/<pre-deploy-file>.db

cp backups/<pre-deploy-file>.db procam_crm.db.restoring
mv procam_crm.db.restoring procam_crm.db
rm -f procam_crm.db-journal procam_crm.db-wal procam_crm.db-shm
chmod 600 procam_crm.db

git checkout <good-hash>
sudo systemctl start procam-crm
curl -s http://127.0.0.1:8002/healthz
.venv/bin/python scripts/production_preflight.py
```

Confirm the file owner matches the service user
(`systemctl show procam-crm -p User`); fix with
`sudo chown procamapp:procamapp procam_crm.db` if needed.

## C. Undo an email recovery run

`scripts/2026_09_22_recover_lost_emails.py` writes every value it is
about to replace to `backups/recovery_preimage_<timestamp>.json` before
committing. To put them back:

```bash
.venv/bin/python scripts/2026_09_22_recover_lost_emails.py \
    --rollback backups/recovery_preimage_<timestamp>.json
.venv/bin/python scripts/email_recovery_report.py
```

Leads that someone has edited since the run are reported and left alone.

## D. A migration's `--down`

Each migration script has `--down --yes`. It drops what the migration
added, **and the data in it**. Almost never needed, because rollback A
already works with the extra columns in place.

| Script | `--down` drops | Loses |
|---|---|---|
| `2026_10_02_vertical_confidence.py` | `leads.vertical_confidence`, `vertical_reason` | why each lead got its vertical |
| `2026_10_01_note_revisions.py` | `lead_notes.revisions` | every note's edit history |
| `2026_09_30_copilot_log.py` | **the whole `copilot_log` and `copilot_chunk` tables** | the Copilot audit log and search index |

Never use `2026_09_30_copilot_log.py --down` to remove only the `answer`
column — it drops both tables. Note also that the boot autoheal re-adds
dropped columns on the next restart while the code still declares them,
so `--down` only sticks together with a code rollback.

## After any rollback

1. `production_preflight.py` shows no FAIL.
2. Smoke test (Deployment Guide step 7).
3. Write down what happened, which rollback, which backup file, and what
   data (if any) was lost.
