# CRM-TIDY-01 — four small things

18 October 2026. Four items, one release. Nothing here is behind a
feature switch: three of them are visible the moment the service
restarts, and the fourth turned out not to exist.

---

## 1. The 156 stale failed emails

Between 4 and 12 October, 156 messages failed with no SMTP code and
the same useless error — "the mail server refused it" — because the
CRM had no working sender until the 13th. They are stale alerts about
work from two weeks ago.

They are marked **cancelled**: not deleted, not resent.

* *Deleted* loses the evidence that the message ever existed. The
  outbox is the only record that somebody should have been told
  something and was not.
* *Resent* puts a fortnight of stale alerts into people's inboxes in
  one morning, which is the opposite of the problem the 13 October fix
  solved.

Only rows that carry **no SMTP code** and were **created before 13
October** are touched. Anything that failed since then was graded by a
mail server that said why, and those are a live problem rather than
history.

```
.venv/bin/python scripts/cancel_stale_failed_mail.py --check
.venv/bin/python scripts/cancel_stale_failed_mail.py --apply
```

`--check` prints the count, the date range, the reasons and the events
and writes nothing. Running `--apply` twice cancels nothing the second
time.

---

## 2. The Overview tiles and the Workbench now agree

### What was wrong

The Overview / Sales Intelligence panel held five SQL queries, written
in `/api/my-work`. The Workbench held predicates for almost the same
five things, written in `app/workbench/filters.py`. They did not mean
the same things:

| Tile | What the Overview counted | What the Workbench counted |
|---|---|---|
| Overdue | `followup_date < today` | any date gone by on anything still open — a follow-up, a quote past its validity, an account action |
| Idle 3+ days | no contact for **3** days | the registry had `idle` at **7** days and `stale` at **30**; three days had no name at all |
| Never actioned | no call, mail or meeting | nothing |
| Awaiting quote | `stage = 'RFQ Generated'` | RFQ Generated **and no quote sent yet** |
| In negotiation | Quoted or Under Negotiation | `quotations`, which also includes RFQ Generated |

So a quote a customer had been sitting on past its validity counted on
one screen and not the other, and the two screens could not be
reconciled by reading either of them.

### What it does now

All six tiles come from the registry, through the same `board()` call
the Workbench draws. Three new keys were added for the views the
Workbench never had — `quiet_3d`, `never_actioned`, `in_negotiation` —
and they carry an **overview** label rather than a tile or a chip: the
Workbench is not getting three new tiles, it is getting three filters
its sibling screen can link to.

**Clicking a tile opens the Workbench on that filter**, and the list
has exactly as many rows as the tile said. That is asserted in the
tests, for an admin and for two individual people, because a count
that is right for an admin and wrong for a rep is still wrong.

### One change worth knowing about

The board used to drop leads that tripped no condition at all. That
was right while it was the only reader — a lead contacted on Monday
with a follow-up booked for Friday needs nothing. It became wrong the
moment a second screen counted the same leads, because "In
negotiation" and "Idle 3+ days" both include perfectly healthy leads.

The board now keeps them. A lead with nothing outstanding matches no
chip but **Everything**, so the only Workbench number that moves is
that one — and it was the number that was wrong to leave them out.
They sort last, under the priority order that was already there.

### Before and after

Run on production **after** deploying, as Admin and as two named
users:

```
curl -s -b cookies 'https://procamlogitech.com/CRM/api/my-work' | python3 -m json.tool
```

and compare `pending` against the Workbench's own `counts`:

```
curl -s -b cookies 'https://procamlogitech.com/CRM/api/workbench?per_page=1' | python3 -m json.tool
```

The six keys that must now be equal:

| Overview `pending` | Workbench `counts` |
|---|---|
| `overdue` | `overdue` |
| `due_today` | `due_today` |
| `untouched_3d` | `quiet_3d` |
| `never_actioned` | `never_actioned` |
| `awaiting_quote` | `rfqs_awaiting_quote` |
| `in_negotiation` | `in_negotiation` |

---

## 3. Mode — Sea / Air / Multimodal

A new optional field on Freight Forwarding leads.

Until 18 October the vertical itself carried this: Sea Freight and Air
Freight were verticals, so a forwarding job had to be filed as one or
the other and a job that was both had nowhere to go.
CRM-VERTICAL-MASTER-01 folded the pair into Freight Forwarding, which
is right — it is one team, one head and one P&L — and lost the
distinction, which was not. Sea versus air is a property of the
shipment, not of the organisation, so it now lives on the shipment,
where **Multimodal** is also sayable for the first time.

* Optional. Blank means nobody has said, not that the job is neither.
* Shown on the lead form only when the vertical is Freight Forwarding,
  and cleared by the server if a lead is reclassified out of it — the
  change is audited like any other field.
* A filter on the Workbench (`?mode=Sea`) and on reports
  (`/api/dashboard/summary?mode=Air`), with its own breakdown under
  `distributions.modes`.
* The vocabulary is served from `/api/dashboard/options` and
  `/api/workbench/filters`, so the page holds no second copy of it.

### Migration

```
.venv/bin/python scripts/2026_10_19_lead_mode.py --check
.venv/bin/python scripts/2026_10_19_lead_mode.py
.venv/bin/python scripts/2026_10_19_lead_mode.py --check     # twice
```

One nullable column. `--down --yes` drops it again.

### Backfill

The audit trail recorded every Sea Freight → Freight Forwarding and
Air Freight → Freight Forwarding move, with the old value. That is the
only place the answer still exists, so the backfill reads it back:

```
.venv/bin/python scripts/backfill_lead_mode.py --check
.venv/bin/python scripts/backfill_lead_mode.py --apply
```

It writes only where **all three** hold: the lead is Freight
Forwarding *now*, its mode is empty, and its most recent vertical
change away from one of the old verticals says which. Everything else
stays blank. Nothing is guessed from the lead title or the account —
that would turn a gap into a wrong answer that looks like a right one.

---

## 4. The "check Vertical before saving" warning

**There isn't one.** Searched the templates, the Python, the docs and
the `help_articles` and `help_tooltips` tables on production: nothing
anywhere asks anyone to check the vertical before saving.

The thing that probably prompted the item is real but is not a
warning: the lead form's vertical `<select>` used to carry four
options while leads carry eleven, so opening a lead whose vertical was
not in the list silently selected option zero and saved it. That was
fixed in CRM-VERTICAL-FIX-01 — the select is built from the master
list, the form sends what it was rendered with, and a save that would
move a vertical nobody touched is refused with a line explaining why.

A test now asserts the warning's absence, so if somebody adds one
later they find out from the suite rather than from a user asking what
the CRM wants them to check.

---

## Deploy

Push first; then, on the server:

```
cd /var/www/procam-crm
git pull
.venv/bin/python scripts/2026_10_19_lead_mode.py --check
.venv/bin/python scripts/2026_10_19_lead_mode.py
.venv/bin/python scripts/2026_10_19_lead_mode.py --check
sudo systemctl restart procam-crm
.venv/bin/python scripts/cancel_stale_failed_mail.py --check
.venv/bin/python scripts/backfill_lead_mode.py --check
```

The restart comes before the two data scripts so that the autoheal has
added the column under the running service either way. Both scripts
are `--check` here: neither writes until you say so.
