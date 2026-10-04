# Runbook — email notifications, the original RFQ, and the weekly pack

Everything in this release is additive and every new behaviour is
behind a flag that is off until somebody turns it on. With no flags
set, applying the migration and deploying the code changes nothing a
person would notice — except one bug fix, which is explained in §2 and
is not optional.

---

## 1. Order of operations

```bash
cd /var/www/procam-crm
git pull
.venv/bin/python scripts/2026_10_11_email_notifications.py --check
.venv/bin/python scripts/2026_10_11_email_notifications.py
sudo systemctl restart procam-crm
```

Then, and only then, turn things on — one at a time, in this order.
Each step is independently reversible by unsetting its flag and
restarting.

| Step | Flag | What starts happening | Reverse |
|---|---|---|---|
| 1 | `FEATURE_RFQ_CAPTURE=true` | New leads keep the client's original message as a `.eml` | unset; the files already captured stay and stay downloadable |
| 2 | `FEATURE_EMAIL_NOTIFY=true` + the outbox timer | Notification email is queued and sent by a worker, with the original attached | unset; mail goes direct again, as it did before |
| 3 | `FEATURE_WEEKLY_PACK=true` + its two timers | Thursday pack, Friday freeze | unset; the timers run, find the flag off and exit |

`FEATURE_DAILY_BRIEF` exists for completeness. The daily brief already
runs and is **not** gated — a flag must add behaviour, never remove a
report people already rely on.

---

## 2. The bug fix that is not optional

The webhook path — the one production runs — called
`save_attachments_for_lead` and discarded the result, so attachment
files were written to disk with **no database row**. With no row there
is no download route, so nothing could reach them: not the lead drawer,
not the API, not the text extraction that reads attachments to work out
a vertical. The five-minute poll recorded them properly, but it skips a
lead the webhook already created, so it never filled the gap.

Both paths now call `app/services/rfq_capture.capture_for_lead`, which
writes the rows. This takes effect at the restart, with no flag.

Historical leads are a separate run — see §5.

---

## 3. Installing the timers

```bash
cd /var/www/procam-crm/docs/operations/deploy
for j in outbox weekly-pack weekly-freeze; do
  sudo cp procam-crm-$j.service procam-crm-$j.timer /etc/systemd/system/
done
# The daily report moved from 08:00 to 08:30 IST and the DQ snapshot
# moved to 02:30 UTC so the two no longer share a minute.
sudo cp procam-crm-daily-report.timer procam-crm-weekly-report.timer \
        procam-crm-dq-snapshot.timer /etc/systemd/system/
sudo systemctl daemon-reload
for j in outbox weekly-pack weekly-freeze; do
  sudo systemctl enable --now procam-crm-$j.timer
done
sudo systemctl restart procam-crm-daily-report.timer \
     procam-crm-weekly-report.timer procam-crm-dq-snapshot.timer
systemctl list-timers 'procam-crm-*' --all
```

The whole schedule, in IST:

| | When | What |
|---|---|---|
| backup | 07:00 | database |
| copilot index | 07:45 | search index |
| DQ snapshot | 08:00 | data quality counts |
| **daily brief** | **Mon–Sat 08:30** | each person's actions |
| weekly, person + head | Mon 09:10 | the week against the one before |
| management report | 1st, 09:00 | company-wide |
| **review pack** | **Thu 14:00** | before the review |
| **Friday freeze** | **Fri 09:00** | the week written down, then sent |
| exceptions | Mon–Sat 19:00 | what is still open |
| **outbox** | **every 2 min** | sends the queue |
| escalation, SLA | every 15 min | the ladders |
| mailbox poll | every 5 min | the safety net under the webhook |

---

## 4. Before turning the email on

```bash
# 1. Does the mail server accept anything at all? Mail.Send is the
#    outstanding IT grant; without it everything below is refused.
.venv/bin/python scripts/check_mail_send.py

# 2. What would be sent, to whom, without sending it.
.venv/bin/python scripts/send_reports.py --daily --dry-run
.venv/bin/python scripts/send_reports.py --weekly-pack --dry-run

# 3. One real one, to yourself.
.venv/bin/python scripts/send_reports.py --daily --to <YOUR-CODE>

# 4. The queue, empty.
.venv/bin/python scripts/outbox_worker.py --status
```

---

## 5. Capturing the originals of leads already in the CRM

Slow, talks to Graph once or twice per lead, resumable, newest first.
Watch the first run.

```bash
.venv/bin/python scripts/backfill_rfq_capture.py --check --limit 20
.venv/bin/python scripts/backfill_rfq_capture.py --limit 50
# then, in batches, over a few evenings
.venv/bin/python scripts/backfill_rfq_capture.py --limit 500
```

`missing` is the expected answer for older leads: the message has been
moved out of the Inbox or is past the mailbox's retention. It is
recorded once and not retried, so each run makes progress and the loop
below terminates:

```bash
# repeat until it reports "0 lead(s) to try"
for i in $(seq 1 20); do
  .venv/bin/python scripts/backfill_rfq_capture.py --limit 500 2>&1 | tail -2
done
```

A run that reports `failed` rather than `missing` is a different
thing: something went wrong that is worth reading in the output. A run
that reports the same `failed` count twice over has stalled, and the
leads causing it are in the output by id.

`failed` is not retried either, until asked:

```bash
sqlite3 procam_crm.db "SELECT status, COUNT(*), substr(error,1,80)
  FROM lead_raw_emails GROUP BY status, substr(error,1,80);"
.venv/bin/python scripts/backfill_rfq_capture.py --retry-failed --limit 50
```

A message over `CRM_MAX_RAW_EMAIL_BYTES` (40 MB) is recorded as failed
with its size in the error. Its attachments are stored regardless —
only the `.eml` is skipped — so raising the cap recovers the envelope,
not the documents.

### The files already on disk

The webhook saved every attachment and recorded none of them, so on
production **2,847 files — 905 MB** were sitting in the leads' own
directories with nothing able to serve them, against 277 that had a
row. The back-fill recovers whatever Microsoft still holds. For the
rest, the file on disk is the only copy and can be adopted without the
network:

```bash
# always look first
.venv/bin/python scripts/find_orphan_attachments.py
.venv/bin/python scripts/adopt_orphan_attachments.py
# then, when the breakdown looks right
.venv/bin/python scripts/adopt_orphan_attachments.py --apply --yes
```

Run the back-fill **first** wherever it can reach, because it also
recovers the original `.eml`, which adoption cannot.

Images embedded in the message body are in there too, and on
production they are most of it: 772 of the first 1,267 adoptable files
were `image001.png` and friends — signature logos and pasted
screenshots. Nine of those on a lead whose real attachment is one gate
pass is worse than none, so leave them out:

```bash
.venv/bin/python scripts/adopt_orphan_attachments.py --apply --yes \
    --skip-inline-images
```

That filters on the **name**, not the extension, which matters here: a
photograph of the cargo often is the document, and `.jpg` is what both
a signature logo and a camera produce. `image001.jpg` is Outlook's;
`IMG_0362.jpeg` is somebody's phone. `--exclude-ext` and `--min-bytes`
are still there for a case this does not cover.

Adoption never invents a lead: a directory whose lead has been deleted
is reported and left, which is what `find_orphan_attachments.py
--delete` is for, and that is a separate decision.

---

## 6. The rule that cannot be turned off from a screen

**The CRM never emails an address outside Procam.** Every recipient is
checked against `app/services/mail_policy.py` in front of the one
transport, so no call site can route around it. Some of these emails
carry a client's own documents; one of them reaching a customer would
be an incident.

- Allowed: `@procamlogistics.com`, `@procamgroup.in`, and their
  subdomains.
- To add a domain: `CRM_INTERNAL_EMAIL_DOMAINS=procam.co.uk,…`
- To lift the block entirely: `CRM_EMAIL_ALLOW_EXTERNAL=true`. It
  exists so a future, deliberate decision does not need a code change.
  Leave it unset.

A refused address is logged at ERROR and shown on **Email Health**. If
a colleague stops getting mail, that screen is the first place to look
— their address on file may be a personal one.

---

## 7. When something goes wrong

| Symptom | Where to look | Likely cause |
|---|---|---|
| Nobody is getting any email | `/admin/email/health` → Failed | `Mail.Send` not granted; every send 403s |
| Queue is growing, nothing sent | `systemctl status procam-crm-outbox.timer` | timer not installed or not enabled |
| "Waiting" high, "Oldest" large | Email Health | the worker has not run, or a lease is stuck held |
| One person gets nothing | `/me/notifications` as them | they have muted it, or quiet hours are holding it |
| Email arrives without the original | the lead drawer | nothing was captured for that lead — see §5 |
| An attachment is named but missing | the journal | the file is no longer on disk; the email still goes |

Useful commands:

```bash
.venv/bin/python scripts/outbox_worker.py --status --json
.venv/bin/python scripts/outbox_worker.py --dry-run
journalctl -u procam-crm-outbox -n 100 --no-pager
journalctl -u procam-crm -n 200 --no-pager | grep -i 'REFUSED external'
```

A failed message can be retried or stopped from **Email Health**;
both actions are audited.

### A stuck lease

A worker killed mid-pass leaves its lease held. It expires by itself
in five minutes. Rows it had claimed come back to the queue after
thirty (`outbox.STUCK_MINUTES`). Nothing needs doing; if it must be
cleared sooner, the lease row is in `job_leases`.

---

## 8. Rollback

In increasing order of severity. The first three lose nothing.

1. Unset a flag in `.env`, `sudo systemctl restart procam-crm`.
   Behaviour reverts; everything captured stays.
2. `sudo systemctl disable --now procam-crm-outbox.timer` — queued mail
   stops going out and waits.
3. `git checkout <previous commit>` and restart. The new tables stop
   being read.
4. Only if the schema itself must go:
   `scripts/2026_10_11_email_notifications.py --down --yes`. This
   discards the queue, every person's preferences, the frozen weeks,
   and the record of which originals were captured — the `.eml` files
   stay on disk but nothing can find them.

---

## 9. What this release does not do

- It does not grant `Mail.Send`. Until IT does, every email is refused
  at the transport, the refusal is recorded, and the in-app bell is the
  only channel that works.
- It does not email customers, under any flag short of
  `CRM_EMAIL_ALLOW_EXTERNAL`.
- It does not change the intake classification, the pipeline stages, or
  the output of any report that existed before it.
