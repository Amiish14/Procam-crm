# Deployment Guide

How to ship a new version to production, repeatably. Every step is safe
to re-run: migrations are additive and idempotent, the boot adds any
column the code needs, and nothing here deletes data.

**Two machines, two separate steps.** Push from the Mac. Only after the
push has finished, pull on the server. Doing both in one breath has
deployed a stale commit before.

---

## 0. Before you start

- Everything is committed on the Mac and the full test suite passes:
  ```bash
  cd ~/Desktop/Procam-crm-main
  .venv/bin/python -m pytest -q -p no:warnings | tail -1
  ```
  The last line must say `passed` with no `failed`.
- You know which commit production is on (step 2 prints it) and what
  migrations lie between it and the new one (step 4 lists them).
- It is not the middle of the working day, or you have told the team.

## 1. Mac — push

```bash
cd ~/Desktop/Procam-crm-main
git status -sb                  # must be clean, on main
git push origin main
git log --oneline -1 origin/main
```

Note the commit hash printed last. **Wait for the push to finish** before
step 2.

## 2. Server — preflight and backup (before touching code)

```bash
ssh procam-app
cd /var/www/procam-crm
git log --oneline -1                                  # what is running now
.venv/bin/python scripts/production_preflight.py      # read-only
.venv/bin/python scripts/backup_database.py --label pre-deploy
```

- Preflight `FAIL`s that predate this deploy (for example a missing
  secret) must be understood before continuing. Schema `FAIL`s here mean
  production already has drift — stop and read the Troubleshooting Guide.
- The backup must end with `check    ok`. If it does not, **stop**.

## 3. Server — pull

```bash
git fetch origin
git log --oneline HEAD..origin/main       # exactly the commits you pushed
git pull --ff-only origin main
git log --oneline -1                      # must match the hash from step 1
```

`--ff-only` refuses if the server has local changes. The server is
pull-only: never `git add`, `commit` or `push` here. If the pull is
refused, see Troubleshooting → "git pull refused".

## 4. Server — migrations

List the migration scripts added by this deploy:

```bash
git diff --name-only <old-hash> HEAD -- scripts/ | grep -E 'scripts/20[0-9]{2}_'
```

For **each**, in filename order: dry run, read it, apply, check again.

```bash
.venv/bin/python scripts/<migration>.py --check
.venv/bin/python scripts/<migration>.py
.venv/bin/python scripts/<migration>.py --check     # now reports nothing to do
```

Migrations in the 2026-09/10 final-audit release (all add nullable
columns only; the boot also adds them, so a restart before this step
does not break pages):

| Script | Adds |
|---|---|
| `scripts/2026_09_30_copilot_log.py` | `copilot_log`, `copilot_chunk` tables; `copilot_log.answer` |
| `scripts/2026_10_01_note_revisions.py` | `lead_notes.revisions` |
| `scripts/2026_10_02_vertical_confidence.py` | `leads.vertical_confidence`, `leads.vertical_reason` |

The production-readiness release adds **no** migrations.

### Release notes — production hardening (2026-10)

**Before `git pull` — private data files.** This release removes three
files holding personal data from the repository. `git pull` deletes them
from the server's working tree. Copy them first:

```bash
cd /var/www/procam-crm
mkdir -p data/private && chmod 700 data/private
cp -p data/employee_directory.csv data/employee_emails_2026_09_02.txt data/private/ 2>/dev/null
cp -p data/imports/*backlog*.xlsx data/private/presales_backlog.xlsx 2>/dev/null
ls -l data/private/
```

**Migration** (after the backup and pull):

```bash
.venv/bin/python scripts/2026_10_04_production_hardening.py --check
.venv/bin/python scripts/2026_10_04_production_hardening.py
```

It adds `audit_events`, `data_quality_snapshots`, sign-in and session
columns on `employees`, Vendor Master columns on `vendor_domains`, and
lead/contact/opportunity indexes. The boot creates the same, so the
order is not critical.

**Indexes on older tables.** Boot and migrations add an index only when
they create its table, so a table older than one of its model's indexes
never gets it; the preflight then warns "indexes N missing". Add them
(additive; unique indexes are listed, not created):

```bash
.venv/bin/python scripts/ensure_model_indexes.py --check
.venv/bin/python scripts/ensure_model_indexes.py
```

On the 2026-09-14 production database this adds 24 indexes on
`companies`, `copilot_chunk` and `copilot_log`. SQLite holds a write lock
while each is built; run it outside office hours or accept a few seconds
of queued saves.

**Behaviour users will notice**

- The Content Security Policy is enforced. If a page stops loading a
  script or image, set `CSP_MODE=report-only` in `.env`, restart, and
  report the page.
- Sessions: role changes apply at once; password resets and
  deactivation end sessions; eight wrong passwords lock an account for
  15 minutes.
- Access follows the Access Matrix everywhere, including pre-sales, RFQ,
  quote, handover and Company 360 views. Check the matrix for anyone who
  relied on the role name alone.
- Operations and Finance departments see all handovers; others see
  handovers of their deals. Grant **All handovers** in the matrix if
  someone else needs the whole queue.

**After the restart**

```bash
.venv/bin/python scripts/build_copilot_index.py
.venv/bin/python scripts/production_preflight.py
.venv/bin/python scripts/ops_status.py --no-network
```

Install the new timers (`procam-crm-ops-status`, `procam-crm-dq-snapshot`)
from `docs/operations/deploy/`.

## 5. Server — restart and verify

```bash
sudo systemctl restart procam-crm
sleep 3
systemctl is-active procam-crm                          # active
curl -s http://127.0.0.1:8002/healthz                   # {"ok":true}
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8002/login   # 200
journalctl -u procam-crm --since "5 min ago" | grep -E "autoheal FAILED|Traceback|ERROR" || echo "clean boot"
.venv/bin/python scripts/production_preflight.py
```

Preflight must show `0 fail` for **schema**. If the service does not come
back, go straight to the [Rollback Guide](ROLLBACK_GUIDE.md).

## 6. Server — post-deploy tasks for this release

```bash
# Notes were never indexed for Copilot search before this release.
.venv/bin/python scripts/build_copilot_index.py --check
.venv/bin/python scripts/build_copilot_index.py

# Accounts still opening with a guessable password (read-only).
.venv/bin/python scripts/audit_default_passwords.py

# Email recovery status (read-only).
.venv/bin/python scripts/email_recovery_report.py
```

## 7. Smoke test in a browser (5 minutes)

As an ordinary sales user and as an admin:

1. Log in → My Work loads with numbers.
2. Open a lead → Email trail, Notes, History drawer (a reassignment shows
   "REASSIGNED · A → B").
3. Add a note, edit it → the note shows as edited.
4. Copilot: "my open leads" → rows with clickable chips; click an
   account chip → opens `/CRM/companies/<id>`.
5. Intake → Review queue → each item says "Classified **<class name>**".
6. Academy → a self-check answers.
7. As the sales user, open another rep's opportunity by URL
   (`/CRM/app?opp=<id>`) → "not found".

## 8. Record the deploy

Note in the team channel: date, old → new commit, migrations run, backup
file name, preflight result. The backup file name is what a rollback
needs.

---

## Repeating a deploy

Re-running any step is safe:

- `git pull --ff-only` on an up-to-date tree does nothing.
- Every migration's `--check` reports "already present" and the apply
  is a no-op.
- The boot autoheal skips existing columns silently.
- A second backup is just another file.

## What never happens on the server

- `git add`, `git commit`, `git push`, `git reset --hard`, `git checkout -- .`
- API keys or secrets in any file other than `.env`
- A migration without a backup taken in the same session
- `--apply`/deletion scripts without first reading their dry-run output
