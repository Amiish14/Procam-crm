# Step 0 — what is already there

Written before any of the notification work, by reading the code rather
than the documentation. Every claim below carries the file and line it
came from, because three of the assumptions in the brief turned out to
be wrong about this deployment and the design had to change.

---

## 0. The four corrections

The brief describes a different machine from the one running.

| The brief says | Production actually is | What that changed |
|---|---|---|
| PostgreSQL | **SQLite**, one file at `/var/www/procam-crm/procam_crm.db` (`docs/DATABASE.md:7-13`) | `SELECT … FOR UPDATE SKIP LOCKED` and `pg_try_advisory_lock` do not exist. The outbox claims rows with a conditional `UPDATE … WHERE status='queued'`, which is atomic because SQLite serialises writers, and single-runner jobs take a `job_leases` row with an expiry instead of an advisory lock (`app/services/leases.py`). |
| Azure App Service + Blob Storage | An **Azure VM**: gunicorn, 2 sync workers, systemd, nginx at `/CRM`, port 8002 (`DEPLOY.md:16,95-115`). No storage account exists anywhere in the repository. | `.eml` files and attachments go to local disk under `EMAIL_INGEST_STORAGE_ROOT`, served by a permission-checked Flask route, never as a static file. The read path is one function, so moving to Blob later is a storage driver and not a rewrite. |
| Alembic | **No migration framework at all**. ~60 hand-written dated scripts in `scripts/`, each with `--check` and `--down` | The migration is `scripts/2026_10_11_email_notifications.py`, in that shape. It creates tables from the models, because the hand-written DDL in earlier migrations is what left 43 declared indexes missing on production. |
| (unstated) | There is **no scheduler in the process** — no APScheduler, no Celery, no cron. Everything is a systemd timer. | The outbox needs its own timer (`procam-crm-outbox.timer`, every two minutes). Nothing can be scheduled from inside the app. |

---

## 1. Inbound mail

Microsoft Graph only; there is no IMAP anywhere in the repository.

- `email_ingest/graph_client.py:49` — MSAL client credentials, app-only.
- Two modes, `EMAIL_INGESTION_MODE`, **default `mailbox`** — the webhook
  (`email_ingest/service.py:31-46`). The canonical inbox is
  `leads@procamgroup.in` (`service.py:40`).
- The webhook fetches the message and calls
  `single_message.process_single_message` (`webhook.py:117`).
- A five-minute poll timer also runs in production as the safety net
  under the webhook (`docs/operations/deploy/README.md:26`).

### What was kept, and what was not

| Kept | Where |
|---|---|
| `Lead.email_message_id`, `conversation_id`, `in_reply_to`, `references_header` | `single_message.py:447-460` |
| subject, sender, received time, body **truncated to 8000 characters** | `single_message.py:119` |
| the thread, as `LeadEmail` rows | `email_ingest/trail.py:16` |
| every intake decision | `EmailClassification`, `app.py:1024` |
| attachment **files** on disk | `email_ingest/attachments.py:47` |

| Not kept | Consequence |
|---|---|
| **The raw MIME.** `$value` / `mimeContent` is never requested — `DEFAULT_SELECT` at `graph_client.py:42-46` | There is no original to forward. It has to be fetched from Graph, now or later. |
| **The attachment rows, on the webhook path.** `single_message.py:479-485` called `save_attachments_for_lead` and threw the return value away; `pipeline.py:568-596` created a `LeadAttachment` for each file | The files were written to `EMAIL_INGEST_STORAGE_ROOT/<lead_id>/` with no database row — so no download route could reach them, the lead drawer showed nothing, and `attachment_text.for_lead` found nothing to read. The webhook is the production path, so this is most of the "I have to ask a colleague for the original" complaint, and it is a bug rather than a missing feature. |

### Re-fetching is possible

`Lead.email_message_id` holds the RFC-5322 id, not Graph's.
`webhook.py:92 _get_message_by_internet_id` turns one into the other
with `$filter=internetMessageId eq '…'`, and
`graph_client.py:221 list_attachments` takes it from there. The
precedent is `scripts/2026_09_22_recover_lost_emails.py`. Only
`Mail.Read` is needed, which is granted. The failure mode is a message
moved out of the Inbox or past retention.

---

## 2. Outbound mail

One transport: `email_ingest/notifier.py:59 send()` →
`POST /users/{from}/sendMail`, HTML body only.

- **No attachment has ever been sent.** No `attachments` key, no
  `createUploadSession` anywhere in the repository.
- **`Mail.Send` is not granted.** The token carries `Mail.Read` only;
  a send returns 403 and is logged (`notifier.py:108-115`). Every
  notification email in production is currently refused at this line.
- `app/services/notify.py:50` is the single entry point above it
  (in-app row + email + audit + dedupe, never raises).
- `app/services/notification_rules.py` holds the matrix: **26 events,
  13 of which had a call site**.
- `scripts/send_reports.py` sends the five scheduled digests.
- `/api/outreach/generate` writes an `OutreachDraft` and sends nothing
  (`app.py:4117`).

---

## 3. What already existed of "Part B"

Most of the reporting in the brief is built. It needed extending, not
writing:

| Already there | File |
|---|---|
| Five report builders, each reading the person's own Workbench board | `app/services/digests.py` |
| The sender, recipient rules and a per-run cap | `scripts/send_reports.py` |
| Four report timers plus escalation and SLA sweeps | `docs/operations/deploy/` |
| The action matrix, thresholds, ageing buckets and `business_today()` | `app/services/sales_rules.py` |
| Escalation ladder with its own idempotency key | `app/services/escalation.py`, `app/models/escalation.py` |

What did not exist: the Thursday pack, the Friday freeze, any frozen
numbers, per-person preferences, quiet hours, batching, a queue, a
retry, and any way for an administrator to see what the mail server
did.

---

## 4. Audit as an event source

`AuditEvent` (`app/models/audit.py:46`) is written by a `before_flush`
hook, but **only for the models and fields in `WATCH`**
(`audit_listener.py:33`) — `Lead`, `Opportunity`, `Company`, `Contact`,
`Employee`, `Quote`, `RFQ` and a few others, field by field.
`Query.update()`, `Query.delete()` and raw SQL bypass it entirely
(`audit_listener.py:10-12`), and `LeadEmail`, `LeadAttachment`,
`Notification` and `EmailEvent` are not watched at all.

So the audit trail is a good record of *what a person changed* and a
poor source of *events to notify on*. Events are therefore raised at
the call site, through `notification_rules.dispatch`, as before.

---

## 5. Roles and runtime

- Permissions: `app/access/service.py:27-62`; `admin.email` already
  governs the ingest settings, so it governs the outbound screens too.
- Scope: `app/access/scope.py:41` — `ALL`, `VERTICAL`, `OWN`. Every
  report is built from the recipient's own scope, so a report cannot
  show somebody a record they could not open.
- Runtime: one VM, gunicorn `--workers 2`, SQLite, no in-process
  scheduler, rate limits per worker and in memory.

---

## 6. What this release therefore had to do differently from the brief

1. Fix the lost attachment rows first — it is the largest part of the
   stated pain and it is three lines, not a feature.
2. Fetch and store the `.eml`, because nothing kept it.
3. Teach the one transport to carry attachments, because it never has.
4. Put a hard internal-only recipient check in front of that transport,
   because these emails now carry a client's documents.
5. Build the queue, the lease and the claim on SQLite's terms.
6. Extend the reports that exist rather than writing a second set.
