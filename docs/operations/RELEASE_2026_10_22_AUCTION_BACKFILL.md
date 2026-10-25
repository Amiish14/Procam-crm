# CRM-FORWARD-01b — the auctions that were already here

Classification runs at ingest. The 254 auction and spot-load mails
that arrived before it existed carry no `lead_type`, so nothing
protects them from the junk sweep and nobody owns the live ones.
This marks them.

## What it changes

- `lead_type = 'auction'` and `lead_platform` on all 254, one
  audit row each (`lead.type_backfilled`).
- The 9 open ones go to the configured auction owner. Whoever had
  the lead stays on it as `secondary_owner` — they lose nothing.
- One catch-up list to that owner, with each auction's closing
  date where the mail states one.

## What it does not change

**No stage moves.** 245 of the 254 are closed, many deliberately.
A backfill that reopened them would hand the sales desk 245 dead
leads to re-close. Closed leads also keep the person who closed
them; they are not reassigned.

## The closing date is read, not guessed

Four of the nine live auctions carry a date in the subject. Two
of those are `Eta at Haldia 20.10.26` — the cargo's arrival, not
the auction's deadline. A date is taken only from an auction
context (a reverse-auction line, "best rate up to <date>"); the
rest of the rows say the deadline is in the portal, which is
true. Five of the nine read that way, the Ariba ones among them,
because Ariba keeps the deadline inside the event.

Any deadline already past is flagged for the owner to close as
bid or not bid with a reason — that outcome is what tells us
which lanes are worth bidding next time.

## Before it will run

`admin_settings.auction_lead_owner` must name somebody. Unset, it
refuses to assign rather than inventing an owner: unowned is how
the last 226 were swept into a bulk delete.

## Deploy

    cd /var/www/procam-crm && git pull
    .venv/bin/python scripts/auction_backfill.py --check     # read-only
    .venv/bin/python scripts/auction_backfill.py --apply

Reversible: `--down --yes` clears the type. It cannot restore a
stage, because none was changed.
