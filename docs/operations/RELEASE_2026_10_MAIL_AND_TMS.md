# Release — mail that sends, mail that is logged, and the TMS seam

Deployment and rollback for the 2026-10-13 release. Read
[`docs/notifications_diagnosis.md`](../notifications_diagnosis.md)
first; it explains why the email fix is not what the brief expected.

---

## 1. The short version

**The CRM has never sent an email.** Microsoft Graph `sendMail` needs
a Mail.Send grant the token does not carry, so every notification
since the feature was built has been a 403. The dispatcher was never
broken — it had no transport underneath it.

The TMS sends over SMTP with credentials that work. The CRM now
speaks SMTP too, with the same credentials and its own sender, and
**that is the entire fix**: put `MAIL_*` in the CRM's `.env` and mail
starts working. Mail.Send becomes optional.

Verified on 2026-10-06: connection, STARTTLS and authentication to
`smtp.office365.com:587` as `tms@procamgroup.in` all succeeded.

---

## 2. Deploy

```bash
cd /var/www/procam-crm
.venv/bin/python scripts/backup_database.py
git pull origin main
.venv/bin/python scripts/2026_10_13_ingest_and_integration.py --check
.venv/bin/python scripts/2026_10_13_ingest_and_integration.py
sudo systemctl restart procam-crm
sleep 5 && systemctl is-active procam-crm
curl -s -o /dev/null -w 'nginx %{http_code}\n' https://procamlogitech.com/CRM/login
```

The migration adds three nullable columns to `leads`, backfills
`received_at` from `created_at`, and creates four tables. It leaves
`client_sent_at` empty deliberately — copying `created_at` into it
would claim every historical forward was written the day it was
relayed.

### Then switch the transport on

```bash
# Prove the credentials from the CRM, reading the TMS file rather
# than sourcing it. (Sourcing a .env executes it.)
.venv/bin/python scripts/check_smtp.py --env-file /var/www/procam-lr/.env
.venv/bin/python scripts/check_smtp.py --env-file /var/www/procam-lr/.env \
    --send-to <your-procam-address>
```

When that delivers, add to `/var/www/procam-crm/.env`:

```
MAIL_SERVER=smtp.office365.com
MAIL_PORT=587
MAIL_USE_TLS=true
MAIL_USERNAME=tms@procamgroup.in
MAIL_PASSWORD=<the same value the TMS uses>
CRM_MAIL_FROM=Procam CRM <tms@procamgroup.in>
```

Restart, then confirm on **Admin → Email Log**: the transport line
should read SMTP, and "Send a test to me" should arrive.

Everything already in `email_outbox` drains on the next worker pass.
Nothing has to be replayed by hand — the queue has been recording
what should have been sent all along.

### Timers

```bash
cd /var/www/procam-crm/docs/operations/deploy
sudo cp procam-crm-admin-daily.service procam-crm-admin-daily.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now procam-crm-admin-daily.timer
```

Nothing is sent until an administrator is chosen in **Admin →
Notification Settings**; the job exits cleanly with nobody on the
list, by design.

---

## 3. Rollback

In increasing severity. The first three lose nothing.

1. **Mail only.** Remove the `MAIL_*` lines from `.env` and restart.
   The CRM falls back to Graph, which 403s — i.e. exactly the
   behaviour of the last two years. The queue keeps recording.
2. **Integration only.** Remove `CRM_INTEGRATION_TOKENS` and restart.
   Every integration route answers 503; nothing else changes.
3. **Code.** `git checkout <previous>` and restart. The new tables
   stop being read. The three lead columns stay and are ignored.
4. **Schema.** `scripts/2026_10_13_ingest_and_integration.py --down
   --yes` drops the four tables. It leaves the lead columns in place:
   SQLite cannot drop a column without rebuilding the table, and
   rebuilding a ten-thousand-row `leads` table to remove three
   nullable columns nothing reads is the larger risk.

Reverting the code does **not** undo the `received_at` backfill, and
does not need to — the column is a copy of `created_at` and nothing
older reads it.

---

## 4. What changed that somebody will notice

| | |
|---|---|
| Lead list | ordered by when the enquiry reached us; search covers the whole table, not the newest 300 |
| Lead drawer | "Client sent" where the customer's own date differs |
| A closed lead | reopens when the customer sends another enquiry, with a line in its history |
| An internal forward | becomes a lead instead of vanishing; flagged "To review" when nobody can be identified |
| "Dear Suranjan…" | assigns to Suranjan |
| More → Admin | Mail Ingestion Log, Email Log, Notification Settings |
| Bulk assignment | one summary email each, not one per record |

---

## 5. Known gaps

- **`/api/client-restriction/check` needs a session**, so the TMS
  cannot call it server-to-server yet. See the contract §6.
- **CRM → TMS webhooks are fire-and-forget.** A failure is logged
  with its request id but nothing retries it automatically; the TMS
  should poll as well as listen.
- **`client_sent_at` is empty for every historical lead.** It can
  only be filled by re-reading the original messages, which the
  back-fill could do and currently does not.
