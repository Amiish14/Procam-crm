# Database

For developers and the Infrastructure Team. The full, generated column
listing is [reference/SCHEMA.md](reference/SCHEMA.md); this page explains
how the database is organised and changed.

## Engine

SQLite, one file (`/var/www/procam-crm/procam_crm.db` in production),
opened by every gunicorn worker. Reads run concurrently; writes are
serialised by SQLite's lock, so keep transactions short and never hold
one across a network call. Journal mode is `delete` today; WAL is a
measured +10% under concurrency (Production Readiness Report) and needs
the backup and restore procedures updated before adoption.

SQLite does not enforce foreign keys here (`PRAGMA foreign_keys` is off);
the application deletes dependants explicitly — the single-lead and bulk
delete paths remove notes, emails, attachments, activities, stage and
assignment history and Copilot passages together, because SQLite reuses
a deleted row's id and leftovers would attach to an unrelated new lead.

## Main entities

```
employees ──< access_profiles (one per employee, optional)
    │
    ├─< leads ──< lead_emails        (the customer thread; originals never overwritten)
    │     ├──< lead_notes            (append-only history in revisions)
    │     ├──< lead_activities, lead_attachments
    │     ├──< lead_stage_history, lead_assignment_history
    │     └──< copilot_chunk          (search passages, stamped with owners)
    │
companies ──< contacts
    ├──< opportunities ──< rfqs ──< rate_sourcing_lines
    │                   └─< quotes ──< quote_lines
    │                   └─< won_handovers (PO capture → TMS)
    └──< crm_account_members  (team: also crm_deal_members, crm_lead_members)

email_classifications   every intake decision and its correction (training data)
email_events            webhook / ingest log
vendor_domains          supplier master used by the classifier
audit_events            every business change (see Security)
deletion_audit          snapshots of permanently deleted records
copilot_log             every Copilot question, answer headline, feedback
data_quality_snapshots  daily counts per data-quality check
master_lists / master_items  vocabularies (verticals, industries, …)
task_definitions / task_instances  SLA tasks
```

Ownership is by employee code (`assigned_to`, `secondary_owner`,
`pic_emp_code`, `owner_emp_code`, `lead_driver`, `prepared_by_id`), which
is what the Access Matrix scope filters on.

## Changing the schema

Rules: **additive only** (new tables, new nullable columns, new
indexes); never rename or drop in a release; every change ships with a
migration script and boot support.

1. Add the column or table to the model.
2. Existing tables: add the column to the autoheal list in `init_db`
   (`_adds`) so a restart before the migration cannot break queries
   (a model column the table lacks breaks every query on it). New
   tables: import the model module in `init_db` before `create_all`.
   New indexes on existing tables: add to `BOOT_INDEXES`.
3. Write `scripts/YYYY_MM_DD_<name>.py` with `--check` (dry run),
   apply, and `--down --yes` (explaining what data it destroys). Guard
   against running on an empty database. Prefer generating table DDL
   from the models, as `2026_10_04_production_hardening.py` does.
4. Test: build a database the way production has it before the change,
   run `--check` (writes nothing), apply, run `--check` again (nothing
   to do), and run the preflight schema comparison.
5. Regenerate `docs/reference/SCHEMA.md`.

`scripts/production_preflight.py` compares the live database with the
models on every deploy and fails on missing columns.

## Migrations in the 2026-09/10 releases

| Script | Adds |
|---|---|
| `2026_09_30_copilot_log.py` | `copilot_log`, `copilot_chunk`; `copilot_log.answer` |
| `2026_10_01_note_revisions.py` | `lead_notes.revisions` |
| `2026_10_02_vertical_confidence.py` | `leads.vertical_confidence`, `leads.vertical_reason` |
| `2026_10_04_production_hardening.py` | `audit_events`, `data_quality_snapshots`, four `employees` columns (sessions and sign-in), lead/contact/opportunity indexes, and the columns listed in its `RELEASE_COLUMNS` |

## Backups and integrity

- `scripts/backup_database.py` — online, consistent, verified backup.
- `scripts/production_preflight.py` — `quick_check`, schema drift.
- `scripts/ops_status.py` — restore verification of the newest backup.
- [Backup](operations/BACKUP_GUIDE.md), [Rollback](operations/ROLLBACK_GUIDE.md)
  and [Disaster Recovery](operations/DISASTER_RECOVERY_GUIDE.md) guides.

## Retention

Nothing is deleted automatically. `scripts/intake_retention.py` can trim
stored bodies of non-lead classifications on request. Retention periods
for `audit_events`, `copilot_log`, `email_events` and classification
evidence are a business decision.
