# Release — email notifications with the original RFQ (2026-10)

Everything is additive. No table is altered, no API renamed, no URL
moved, no authentication changed. Four new URLs, five new tables, five
feature flags, and one bug fix that is not behind a flag.

The order to deploy and switch on is in
[the runbook](NOTIFICATIONS_RUNBOOK.md). What was found before building
any of it is in [`docs/notifications_discovery.md`](../notifications_discovery.md).

---

## 1. The problem this release is about

A request arrives at `leads@procamgroup.in` with the drawings attached.
The CRM reads it, creates a lead, assigns it — and the person who has
to quote cannot see the drawings. They ask a colleague to forward the
original.

Three separate causes, all now fixed:

1. On the webhook path — the one production runs — the attachment
   **files** were written to disk and the **database rows were not**.
   With no row there is no download route, so nothing could reach them.
2. The original message was never kept at all. Only a summary and a
   body truncated at 8,000 characters.
3. The one email transport had never sent an attachment.

---

## 2. What a person sees that they did not before

| | Where | Who |
|---|---|---|
| "Original email (.eml)" on a lead | the lead drawer | anyone who can open the lead |
| The files that came with the request | the same card | as above |
| The original and its files attached to the assignment email | their inbox | the new owner and the secondary |
| Email preferences — channels, batching, quiet hours, muting | `/me/notifications` | everyone |
| Notification rules | `/admin/email/rules` | `admin.email` |
| Report schedules and this week's freeze | `/admin/email/schedules` | `admin.email` |
| Email health — queued, failed, refused | `/admin/email/health` | `admin.email` |
| Review pack, Thursday 14:00 | their inbox | heads and management |
| Friday freeze, 09:00 | their inbox | heads and management |

The daily brief moved from 08:00 to **08:30 IST**, and the Monday
weekly from 08:30 to 09:10, so no two reports share a minute. The data
quality snapshot moved to 02:30 UTC for the same reason.

---

## 3. Migration

```bash
.venv/bin/python scripts/2026_10_11_email_notifications.py --check
.venv/bin/python scripts/2026_10_11_email_notifications.py
```

| Table | What it holds |
|---|---|
| `lead_raw_emails` | the client's message, as a `.eml` on disk |
| `email_outbox` | every outbound email, queued, retried, recorded |
| `notification_prefs` | one row per person; no row means the old behaviour |
| `weekly_pipeline_snapshot` | the Friday freeze |
| `job_leases` | which process may run a job — SQLite's answer to an advisory lock |

Nothing is backfilled. `--down --yes` drops all five, which discards
the queue, the preferences and the frozen weeks; the `.eml` files stay
on disk and become unreachable.

Built from the models rather than hand-written DDL, because the
hand-written migrations in this repository are what left 43 declared
indexes missing on production.

---

## 4. Flags

| Flag | Default | Turns on |
|---|---|---|
| `FEATURE_RFQ_CAPTURE` | off | keeping the original `.eml` |
| `FEATURE_EMAIL_NOTIFY` | off | the outbox: queue, retry, batch, health |
| `FEATURE_WEEKLY_PACK` | off | Thursday pack and Friday freeze |
| `FEATURE_REPLY_CAPTURE` | off | a reply's own attachments and original |
| `FEATURE_DAILY_BRIEF` | n/a | the brief already runs and is not gated |

With all of them unset, the only behaviour change in this release is
the attachment-row fix in §1.1, which is a defect and not a feature.

---

## 5. The rule that is not configurable from a screen

**The CRM never emails outside Procam.** Checked in front of the one
transport (`app/services/mail_policy.py`), so no call site can route
around it. These emails now carry clients' documents.

Allowed: `@procamlogistics.com`, `@procamgroup.in` and their
subdomains; extend with `CRM_INTERNAL_EMAIL_DOMAINS`. A refused
address is logged at ERROR and shown on Email Health.

---

## 6. How the concurrency works, since this is SQLite

There is no `FOR UPDATE SKIP LOCKED` and no advisory lock.

- **Idempotency** is `email_outbox.dedupe_key`, unique. A replayed
  webhook, a double-clicked button and two overlapping sweeps all write
  the same key and the second is refused by the index.
- **Claiming** is `UPDATE … SET status='sending' WHERE id IN (…) AND
  status='queued'`. SQLite serialises writers, so exactly one worker
  gets rowcount 1.
- **One runner** is `job_leases`: a row with an expiry, taken by a
  conditional UPDATE. A worker that dies loses its lease in five
  minutes rather than holding it for ever, and rows it had claimed
  return to the queue after thirty.

All three port to PostgreSQL unchanged, if that move happens.

---

## 7. Tests

`tests/test_mail_policy.py` (17), `tests/test_outbox.py` (21),
`tests/test_rfq_capture.py` (9), `tests/test_weekly_pack.py` (7),
`tests/test_notify_outbox_integration.py` (3),
`tests/test_notification_call_sites.py` (4). Full suite: 2,581 passing.

Three defects were found by these tests before anyone saw them: the
preferences page stored `mode` as a boolean so batching never applied;
the review pack counted the Workbench board as the pipeline, which
would have reported a well-run vertical as an empty one; and quiet
hours were being applied to reports addressed to a list rather than a
person.

---

## 8. Rollback

Unset the flag and restart. Nothing captured is lost, the queue holds
whatever has not gone, and the screens go on working against the rows
that are already there. Full ladder in the runbook §8.

---

## 9. Still blocked on somebody

**`Mail.Send` is not granted.** Until IT grants and admin-consents it
on app `bd542cf9-f851-4bcb-b731-70e3e0b2ae42` in tenant
`51b4acb4-b66e-4d12-b62a-0af397564a67`, every email in this release is
refused with a 403 at the transport, recorded as failed on Email
Health, and retried. The in-app bell works regardless. Everything else
here — the capture, the `.eml` button, the queue, the screens, the
freeze — works today without it.
