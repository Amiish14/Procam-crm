# Diagnosis — why CRM notification email does not arrive

Written before any of Part 2 was implemented, by reading the code in
both applications. The brief's working assumption turns out to be
wrong in a way that changes the fix, so this is the part to read
first.

---

## The finding

**The Procam CRM has never successfully sent an email.** Not "the
dispatcher is unreliable" — there is no working transport at all.

- The CRM's only transport is `email_ingest/notifier.py:send()`, which
  calls Microsoft Graph `POST /users/{from}/sendMail`.
- That call needs the **Mail.Send** application permission. The token
  carries `Mail.Read` only — `scripts/check_mail_send.py` confirms it
  against the live tenant, and has reported `CANNOT SEND` every time
  it has been run, most recently 2026-10-04 01:26 UTC.
- So every assignment email, every reminder, every scheduled report
  has been refused with HTTP 403 and logged. The feature was built,
  the code is correct, and nothing has ever left the building.

### The brief's premise does not hold

> "Use the same proven transport/credentials as the working
> password-reset mechanism."

There is no password-reset email in this CRM. Password reset is
`POST /api/employees/<id>/reset-password`, which mints a temporary
password and **returns it in the JSON response** for an administrator
to hand over out of band (`app.py:1794-1800`). Nothing is emailed.
`grep` for `smtplib`, `SMTP`, `flask_mail`, `sendgrid` across the CRM
returns nothing.

### Where the working transport actually is

The **TMS** — the separate Procam LR application — has one, and the
brief naming `tms@procamgroup.in` as the sender is the clue:

| | |
|---|---|
| `app/services/notify.py::_send_email` | plain `smtplib`, STARTTLS, username/password |
| `app/config.py:242-251` | `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USE_TLS`, `MAIL_USERNAME`, `MAIL_PASSWORD` |
| `MAIL_DEFAULT_SENDER` | `Procam TMS <tms@procamgroup.in>` |

That is an SMTP account, not Graph, and it does not need Mail.Send.
It is also text-only and sends no attachments, so the CRM cannot use
that code even if the architecture allowed it — the CRM has to send
HTML and attach the original RFQ.

**What the CRM needs is the credentials, not the code.** Same mailbox,
same SMTP server, its own sender written for HTML and attachments.
That is what `app/services/mailer.py` does, and it is why the fix does
not have to wait for IT.

---

## What already exists, and works

Much of Parts 2, 5 and 6 was built on 2026-10-03 and is already in
production. It needs extending, not writing:

| Asked for | Already there |
|---|---|
| Outbox table, idempotency key, retries | `email_outbox`, `dedupe_key` unique, back-off 2/10/30/120/360 min, 5 attempts |
| A dispatcher that is not a Flask thread | `scripts/outbox_worker.py`, `procam-crm-outbox.timer`, every 2 min, verified re-arming |
| Single-runner safety | `job_leases`, conditional-UPDATE claim (SQLite has no advisory lock) |
| Quiet hours, batching, muted events | `notification_prefs`, `/me/notifications` |
| Email log with status and error | `/admin/email/health` |
| Daily brief, weekly pack, Friday freeze | `scripts/send_reports.py` + four timers |
| Never email outside Procam | `app/services/mail_policy.py`, enforced at the transport |

So: **the dispatcher is not the problem.** It runs, it claims rows, it
reports `sent=0 failed=0` because nothing is queued — and anything it
does claim dies at the Graph 403.

---

## What is not verifiable from the code

These need the production machine, and the commands are in
§"Completing the diagnosis" below:

1. How many rows are in `email_outbox` by status over 14 days.
2. Whether the TMS's `MAIL_*` variables are actually populated in its
   production `.env`, or whether its email is also dormant.
3. Whether that SMTP account permits sending from a second application.

Until (2) is answered, the honest statement is: the CRM's transport
problem has a fix that does not need Microsoft, **if** the TMS mailbox
is genuinely sending today. If it is not, both applications are
waiting on the same thing and Mail.Send is back on the critical path.

---

## Completing the diagnosis

On the server:

```bash
cd /var/www/procam-crm
# 1. the queue, by status, last 14 days
sqlite3 procam_crm.db "
SELECT status, COUNT(*) FROM email_outbox
 WHERE created_at >= datetime('now','-14 days') GROUP BY status;
SELECT COUNT(*) AS all_time FROM email_outbox;"

# 2. what the dispatcher thinks
.venv/bin/python scripts/outbox_worker.py --status
systemctl list-timers procam-crm-outbox.timer --all --no-pager

# 3. is the CRM's Graph token still refused?
.venv/bin/python scripts/check_mail_send.py

# 4. does the TMS have live SMTP credentials? (names only, no values)
grep -o '^MAIL_[A-Z_]*' /var/www/procam-lr/.env 2>/dev/null || \
  echo 'TMS .env not at that path — find it and list the MAIL_* names'

# 5. has the TMS actually delivered anything?
sqlite3 /var/www/procam-lr/*.db "
SELECT status, COUNT(*) FROM outbound_messages GROUP BY status;" 2>/dev/null
```

Answer 4 and 5 and the transport question is settled either way.

---

## The fix, in order of what it unblocks

1. **A transport the CRM owns** (`app/services/mailer.py`) that speaks
   SMTP with the `MAIL_*` credentials and falls back to Graph. One
   service for password reset, notifications, briefs, packs and admin
   reports, as Part 2 §8 asks.
2. Everything already queued then drains on the next worker pass,
   because the outbox has been recording what should have been sent
   all along. Nothing has to be replayed by hand.
3. Mail.Send stops being a blocker and becomes a second option.

The one thing no code can decide: **which mailbox Procam wants CRM
email to come from.** `tms@procamgroup.in` is what the brief says and
what the credentials exist for, but it will read oddly to a
salesperson receiving a CRM lead notification from the TMS address.
`crm@` or `noreply@` on the same server would need one new mailbox and
nothing else.
