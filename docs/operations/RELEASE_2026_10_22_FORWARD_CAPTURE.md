# Who forwarded it, and what kind of enquiry it is

22 October 2026. CRM-FORWARD-01.

## Why

226 SuperProcure load postings arrived in `leads@` between 2 and
11 September. Every one was a forward — `FW: Isgec Heavy
Engineering Ltd. : Load Post Notification` — and every one was
deleted, 138 of them in a bulk review of unassigned leads.

Two things were missing and both of them mattered.

**Nobody could tell they were time-critical.** A spot load closes
in hours and a reverse auction closes at a stated minute. Nothing
on the record said so, and a load posting looks exactly like a
newsletter to somebody scanning a spreadsheet.

**Nobody could say who forwarded them.** The parser unwraps a
forward and records the original sender, which is right — the
enquiry belongs to the customer. The colleague who passed it on
was dropped. When the postings stopped on 11 September there was
no one to ask, and no raw `.eml` survived to recover it from.

## 1. Forwarder capture

At ingest, when the message really is a forward:

| Column | What it holds |
|---|---|
| `forwarded_by_email` / `_name` | the envelope sender — the colleague |
| `forwarded_subject` | the subject as it arrived, `FW:` and all |

`original_email_from` keeps the customer, as before. The lead
shows **"Forwarded by &lt;name&gt;"** on its summary card.

Only on a real forward: detected from the body's header block, or
from an `FW:`/`Fwd:` subject when the client stripped the block.
Otherwise every ordinary enquiry would record its own sender as
having forwarded itself.

### Raw mail retention

`admin_settings.raw_email_retention_days`, **180** by default,
floor of 7. `scripts/raw_email_retention.py` removes the file past
that and **keeps the row** — the row says a message existed, which
is the part somebody looks for; the file is the bulky part with a
customer's whole message in it.

**Nothing is backfilled.** The forwarder was never stored and no
`.eml` survives for the 226. A guess would name the wrong
colleague, which is worse than an empty field.

## 2. Auction / spot load

A match on subject or sender sets `lead_type='auction'` and
`lead_platform`, and:

- **Never junk-swept.** `2026_09_02_purge_irrelevant_leads.py`
  skips them.
- **Never bulk-closed.** `bulk.delete` refuses with the ids.
- **Routed** to the owner in `admin_settings.auction_lead_owner`.
  No name appears in code; a test asserts that.
- **Closing one asks whether we bid.** `bid_outcome` is `bid` or
  `not_bid` and the endpoint refuses without it. "Which lanes do
  we win" cannot be answered from a free-text reason.

Patterns live in the Master Data list `auction_pattern`, each
carrying its platform, so a new portal is a row rather than a
release. Seeded with SuperProcure load posts and auction starts,
Ariba sourcing events, and the generic "reverse auction" /
"auction is now live" / `RA-<n>` shapes.

**The subject and sender only — never the body.** A quotation
that mentions a reverse auction in a paragraph is not an auction
notification, and a rule that read the body would mark it as one.

## 3. Report

```bash
PYTHONPATH=/var/www/procam-crm .venv/bin/python \
  scripts/auction_leads_report.py --check
```

Last 90 days by platform, customer, month and what matched, plus
where those leads are now and how many have a forwarder recorded
(none, before this release).

## Deploy

```bash
cd /var/www/procam-crm && git pull origin main

PYTHONPATH=/var/www/procam-crm .venv/bin/python \
  scripts/2026_10_22_forward_capture.py --check
PYTHONPATH=/var/www/procam-crm .venv/bin/python \
  scripts/2026_10_22_forward_capture.py
sudo systemctl restart procam-crm

# read it, then seed
PYTHONPATH=/var/www/procam-crm .venv/bin/python \
  scripts/auction_leads_report.py --check
PYTHONPATH=/var/www/procam-crm .venv/bin/python \
  scripts/auction_leads_report.py --seed

# the owner — an employee code, not a name
PYTHONPATH=/var/www/procam-crm .venv/bin/python - <<'PY'
from app import app
from app.services import admin_settings
with app.app_context():
    admin_settings.save('auction_lead_owner', 'EMP______',
                        actor='DIR12010')
PY

# retention, if 180 days is not the number you want
PYTHONPATH=/var/www/procam-crm .venv/bin/python \
  scripts/raw_email_retention.py --set-days 180
PYTHONPATH=/var/www/procam-crm .venv/bin/python \
  scripts/raw_email_retention.py --check
```

Additive. Classification runs on new mail at ingest; existing
leads keep an empty `lead_type` until somebody sets one, because
nothing is backfilled.

Rolling back: `--down --yes` on the migration, or empty the
`auction_pattern` list, which stops the marking without a deploy.
