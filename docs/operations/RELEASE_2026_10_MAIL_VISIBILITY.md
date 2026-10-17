# Release — CRM-C2: stale alerts, the 554, and where your mail went

18 October 2026. Five pieces of work. **One migration, additive only
— five columns, no row created, moved or deleted.**

---

## 1. Stale alerts

### The duplicate check never read the words

`notify._recent_duplicate()` keyed on (person, kind, entity,
10 minutes). A second, *different* update to the same lead inside
that window was dropped completely — no bell row, no email — and the
person kept the first version. The health check on 18 October counted
**1,273 records in a fortnight** with more than one notification in
the same hour; every one of those was a chance to have swallowed a
real update.

`notifications.content_hash` now carries a 32-character fingerprint
of the title and body, and it is part of both the bell's duplicate
check and the outbox's dedupe key. A repeat of the same message is
still suppressed. A different one gets through.

Whitespace is collapsed first, so a reflowed body is not a new
message. Rows written before the column existed have a null hash and
match nothing, so the first notification after the migration is
delivered rather than suppressed against a row whose text nobody
recorded.

### Every email was a snapshot

The body was rendered when the event fired and sent unchanged however
much later. Median wait: **69 minutes**. Worst: **nine hours**.

`email_outbox.render_json` now carries what it takes to rebuild the
message — the event, the record, the detail, the actor — and
`outbox.refresh()` re-renders from the record as it stands, just
before the send. If the record has moved since, the body opens with
**"Updated since this alert was raised."**

The event, the actor and the detail are *not* rewritten: the mail
still says what happened and who did it. Only the description of the
record is refreshed, because that is the part that goes stale while a
message waits. A row with no render spec — a report, a digest, a bulk
summary — is sent exactly as queued.

---

## 2. The 554, and the 156 old failures

### Cause, found in the code and reproduced

```
Content-Type: message/rfc822
Content-Transfer-Encoding: base64
```

RFC 2046 §5.2.1: *"No encoding other than 7bit, 8bit, or binary is
permitted for the body of a message/rfc822 entity."* Exchange
enforces it and answers `554 5.6.0 Invalid message content when
handling MIME exception`.

`EmailMessage.add_attachment(bytes, maintype='message',
subtype='rfc822')` base64-encodes the part. That is what
`mailer._attach` did for every captured `.eml`, and RFQ capture
started attaching them on 13 October. The first two rejections are
dated 16 October. The whole message is refused, so the colleague is
told nothing at all.

**Fixed:** a `message/rfc822` attachment is parsed into a Message and
attached as an object, which Python emits as 7bit or 8bit. Bytes that
will not parse — a truncated capture — go as
`application/octet-stream`, still named `.eml`, still opening in
Outlook, and legal to base64. Filenames are stripped of CR, LF and
tab, because a filename goes into a header.

Seven tests in `tests/test_mailer.py` cover that shape.

### The 156 old failures — proposed, NOT applied

They are dated 4–12 October, carry no SMTP code, and all say "the
mail server refused it" — they pre-date both the SMTP grading and the
working sender. 139 are more than a week old.

Proposal:

```sql
-- NOT RUN. For review only.
UPDATE email_outbox
   SET status     = 'cancelled',
       last_error = 'Stale: queued before the 13 Oct SMTP fix. '
                    'Not resent — see RELEASE_2026_10_MAIL_VISIBILITY.'
 WHERE status = 'failed'
   AND smtp_code IS NULL
   AND created_at < '2026-10-13';
```

`cancelled` rather than deleted: the row is the evidence that the
message existed. **Do not resend them.** They describe work from two
weeks ago; delivering them now would put a fortnight of stale alerts
into people's inboxes in one morning, which is the opposite of the
problem this release is fixing.

Inspect them first with `scripts/inspect_mail_failures.py --all`.

---

## 3. Daily brief reach

`scripts/brief_reach.py` — read-only. Every active employee in exactly
one bucket: receives, empty board, owns nothing, no address, bad
address, muted, email off. The two that matter are **no address** and
**bad address**: those people would get the brief if their record were
right. Everything else is the CRM correctly having nothing to say.

---

## 4. CRM-MAIL-VISIBILITY-01

Everything behind a flag, all **off** by default.

| Flag | What it turns on |
|---|---|
| `FEATURE_MY_MAIL` | the `/my-mail` page and its nav badge |
| `FEATURE_FORWARD_RECEIPT` | a receipt to whoever forwarded the mail |
| `FEATURE_REOPEN_ON_MAIL` | reopening a closed lead, and the My Work tile |
| `FEATURE_QUOTE_REPLY_CLASS` | the forwarded-offer class |

**A. My Mail** — `/my-mail`, every user, no permission needed. Every
message the leads mailbox received that is yours: you forwarded it, or
it landed on your lead. Received, sender, subject, outcome, lead,
**stage**, why, and the original as `.eml`. An administrator sees
everything and can filter by forwarder; everybody else sees their own,
checked per row and not per URL.

The Stage column is the point. #11807 was Lost, so an enquiry filed
against it correctly was nowhere Sanjeev would look.

**B. Receipt mail** — one short mail to the forwarder only, never to
the client. Outcome in a line, lead number, company, stage, PIC, and
for a skipped mail the reason plus deep links to create or attach.
Queued `urgent`, so it is not held for a batching window.

**C. Reopen and alert** — a closed lead that receives client mail
comes back **to its last open stage**, read from the stage history. A
deal that was in Under Negotiation when somebody marked it Lost is not
a fresh RFQ, and filing it as one loses its pipeline position and its
place in the forecast. Pre-alerts, B/Ls and booking confirmations
count as client activity even when the classifier calls them a reply —
that is why #11807 stayed Lost through pre-alerts on 12 October.

Closed leads with mail in the last 14 days appear on the owner's My
Work under **New mail on closed lead**. `lead_items` reads open leads
only, which is exactly why they were invisible, so they come from
their own query.

**Not done, deliberately:** the "Shipment activity received — mark
Won?" banner. The signal is built and tested (`mail_signals`), and the
reopen note records it, but the lead-drawer banner is not. See *What
is not in this release*.

**D. Forwarded offers** — a new class `Q_quote_or_reply`. The real
hole was step 4: our own offer, forwarded in, with no lead to attach
to, returned a QUOTE decision with `lead_id=None` — a class that
attaches to nothing and creates nothing, so the message produced no
lead, no note and no row. That is precisely what happened to the
BrahMos offer. It now creates a lead at **Quoted**, with the contact
set from the external party in the chain (never the colleague who
forwarded it), the vertical from the words, and `quote_date` from the
message.

**F. The vertical dropdown** — `#lVt` carried four hard-coded options
against eleven live verticals. Opening a Freight Forwarding lead and
saving *anything* rewrote it to Heavy Transport. 677 leads are on
General Transport and 450 on Freight Forwarding, so this was not a
corner. The list now comes from the same endpoint as the filters, and
the lead's own value is always present and always selected.
`scripts/vertical_rewrite_report.py` lists the suspected damage as a
CSV — **report only**, because which vertical a lead belongs to is a
business judgement.

**§3 data fixes** — `scripts/mail_backfill_report.py`, report mode,
with no `--apply` at all. Rows already held are marked ALREADY HELD by
message id, client address or subject, so #12173 (BrahMos, created by
hand on 13 Oct) cannot be duplicated.

---

## 5. Management vertical head

Not assigned — that is yours. The administrators' morning report now
opens with a red panel naming it, how many records it affects, and the
exact screen and steps. It stays until somebody sets it.

**To set it:** `Admin → Email → Vertical heads` → the **Management**
row → choose a **Head** → **Save**. Effective within 30 seconds, no
restart. Management is the fallback for every unmapped vertical, so
one name covers all ~1,300 affected leads at once.

---

## What is not in this release

- **The "mark Won?" banner** on the lead drawer (spec C, second half).
  The signal exists and is tested; the UI is not built. A one-click
  stage change needs its own confirm path and its own audit entry, and
  I would rather ship the detection than a half-built button that
  changes a stage.
- **Email health alert when Failed > 10 in an hour** (spec E). The
  MIME cause is fixed, which was the live failure; the alert is a
  separate piece.
- **Row actions on My Mail** — Create lead / Attach to lead / Move to
  another lead (spec A). The page lists and explains; it does not yet
  act. The receipt mail's buttons deep-link to it.
- **The 156 stale rows** — proposed above, not applied.

---

## Deploy

```
cd /var/www/procam-crm
git pull
.venv/bin/python scripts/2026_10_18_mail_visibility.py --check
.venv/bin/python scripts/2026_10_18_mail_visibility.py
sudo systemctl restart procam-crm
sleep 10
curl -sS -o /dev/null -w '%{http_code}\n' https://procamlogitech.com/CRM/login
.venv/bin/python scripts/2026_10_18_mail_visibility.py --backfill
```

Migrate, then restart. The backfill runs after the restart because it
reads the columns the migration just added.

Then turn the flags on one at a time, in this order, at
`Admin → Email → Switches`:

1. `FEATURE_MY_MAIL` — read-only, safe immediately.
2. `FEATURE_QUOTE_REPLY_CLASS` — changes what new mail becomes.
3. `FEATURE_REOPEN_ON_MAIL` — changes lead stages.
4. `FEATURE_FORWARD_RECEIPT` — starts sending mail. Last, so the
   first three are known good before anybody is written to.

## Rollback ladder

**1 — A flag misbehaves.** Turn it off at `Admin → Email → Switches`.
Ten seconds, every worker, no restart. Every piece of §4 is behind
one, so this is the whole rollback for the visibility work.

**2 — The re-render is wrong.** Set `email_outbox.render_json = NULL`
for queued rows; they then send their snapshot, which is the old
behaviour.

```sql
UPDATE email_outbox SET render_json = NULL WHERE status = 'queued';
```

**3 — The dedupe change is wrong.** Revert the `notify.py` commit and
restart. The column stays and is simply unread.

**4 — Take the lot off.**

```
git log --oneline -8
git revert --no-edit <commit>        # newest first
sudo systemctl restart procam-crm
```

Revert, do not reset: the branch is shared and pushed. The five
columns stay — they are read with COALESCE everywhere, SQLite cannot
drop a column without rewriting the table, and leaving them costs
nothing.

## Tests

- `tests/test_stale_alerts.py` — 14
- `tests/test_mail_visibility.py` — 28
- `tests/test_mailer.py` — 7 added for the MIME shape

Full suite: **2,897 passing**.
