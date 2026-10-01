# CRM Hygiene Score and quote/RFQ escalation

Two things that keep a CRM honest: a score that says whether it is being
kept properly, and a ladder of reminders that makes sure a customer
waiting for a price is never waiting silently.

They are documented together because they share a principle. Neither
invents a rule of its own. The score is arithmetic over the Data Quality
checks the CRM already runs; the escalation ladder is timing over the
quote deadlines the CRM already keeps. Where a number comes from is
always something a person can open and look at.

| | |
|---|---|
| Score and its arithmetic | `app/services/hygiene.py` |
| The page | `/hygiene`, `templates/hygiene/home.html` |
| Escalation | `app/services/escalation.py` |
| The sweep | `scripts/escalation_sweep.py`, every 15 minutes |
| What each check means | [Data Quality Guide](DATA_QUALITY_GUIDE.md) |

---

## Part 1 — The CRM Hygiene Score

### What the number is

A score out of 100 for one person, one vertical or the whole company.
It is not a judgement of how well somebody sells. It is a measure of
whether the records they are responsible for can be relied upon by
anybody else — the forecast, the handover, the person covering their
leave.

| Band | Score | What it means |
|---|---|---|
| Good | 85 and above | The records can be relied upon. |
| Watch | 65 to 84 | Enough is missing that some reports are wrong. |
| Poor | below 65 | The records are not being kept. |

### How a factor is scored

Each factor is worth a fixed number of points and loses them in
proportion to how much of its population its checks flag:

```
points lost = weight × (records flagged ÷ records that could be flagged)
```

* **Records flagged** is the count from the Data Quality checks named
  below, measured inside the viewer's Access Matrix scope. It is the
  same number the Data Quality dashboard shows for the same checks.
* **Records that could be flagged** is the population the factor applies
  to — open leads, won business, active accounts — counted with the same
  definitions of "open", "live" and "won" that the checks use. A
  different definition here would make the percentage a lie.
* A factor with **nothing in its population deducts nothing**. A
  salesperson with no lost deals is not a salesperson with poor hygiene.
* The share is **capped at 1**, because two checks can flag the same
  record and no factor may cost more than it is worth.
* A check that **cannot be measured** costs nothing and says so on the
  page, rather than being counted as zero problems.

The score is `100 − the sum of the deductions`, floored at 0.

### The factors and their weights

The weights total 100. They are asserted in `app/services/hygiene.py`
and again in `tests/test_hygiene.py`, so changing one here without
changing both is caught.

| Factor | Weight | Computed from (Data Quality checks) | Measured against |
|---|---:|---|---|
| Every record has an owner | 20 | `unowned_leads`, `leads_of_leavers`, `unowned_opps`, `opps_of_leavers`, `no_pic`, `accounts_of_leavers` | live leads + opportunities + active accounts |
| A next action is set | 12 | `leads_no_followup` | open leads |
| Follow-ups are completed | 12 | `stale_leads` | open leads |
| Open deals are kept up to date | 12 | `stale_opps` | open opportunities |
| RFQs reach a quotation | 12 | `rfq_no_quote`, `quoted_without_quote` | RFQs awaiting a quote + leads at a quoting stage |
| Expected close dates are set | 8 | `opps_no_close_date` | open opportunities |
| A loss says what was learned | 4 | `lost_no_competitor` | lost opportunities |
| Won business is valued and handed over | 12 | `won_no_po`, `won_no_handover`, `won_no_value`, `won_leads_no_value` | live handovers + won opportunities + won leads |
| Accounts are contacted | 8 | `inactive_customers` | active accounts |

Why those weights, briefly:

* **Ownership carries the most** because a record nobody owns makes
  every other factor unmeasurable: it appears in no one's work, and no
  reminder, task or escalation ever reaches it.
* **The four middle factors are equal at 12** — a next action, a
  completed follow-up, an up-to-date deal and a quote that actually went
  out are the day's work, and no one of them outranks the others.
* **Won business is 12** because work won but not valued or handed over
  costs the company money, not just tidiness.
* **A loss is worth only 4**: the record is already closed, so the cost
  is the learning, not the deal.

The thresholds inside the checks — 30 days without contact, 45 days
untouched, 12 months inactive — are **not** set here. They live in
`app/data_quality/definitions.py` and are documented in the
[Data Quality Guide](DATA_QUALITY_GUIDE.md). Changing one changes the
Data Quality dashboard and the Hygiene Score together, which is the
point of their living in one place.

### Why every deduction names its records

A score nobody can argue with is a score nobody acts on. Each factor on
`/hygiene` shows the checks it is made of, the count each returned, a
few of the offending records by name, and a link to the rest. The
factor page at `/hygiene/factor/<key>` lists them check by check, with
what each one costs the business and what to do about it, and every row
opens the record itself.

If somebody disagrees with their score, the page shows them exactly
which records to look at. Either the records are wrong and they fix
them, or the check is wrong and the threshold is the thing to argue
about — either way the argument is about something real.

### The three levels

| Level | How | Who may see it |
|---|---|---|
| One person | `/hygiene?for=EMPCODE` | Anyone whose Access Matrix scope reaches that person. |
| One vertical | `/hygiene?vertical=NAME` | Anyone whose scope reaches more than themselves. |
| Everything you can see | `/hygiene` | Everyone. For an unrestricted viewer this is the whole company. |

Narrowing can never widen. Asking about somebody outside your scope is
refused with "that person is outside your access", not answered with an
empty page — so a manager can tell "they have nothing outstanding" apart
from "not yours to see".

One consequence is worth knowing: a record with **no owner at all** is
invisible to a scoped viewer, by the Access Matrix's own design. A
salesperson's score therefore never punishes them for a lead nobody
owns. The company-wide score does, which is where unowned work should be
dealt with anyway.

### Reading it well

* A low score on a **small population** is not a crisis. One open lead
  with no follow-up out of one open lead is 100% of that factor. The
  page shows the flagged count and the population side by side for
  exactly this reason.
* Compare a person against **their own last reading**, not against a
  colleague with a different book of business.
* Use the factor pages as a worklist. Clearing the records is the only
  thing that moves the number, which is the behaviour the score is for.

---

## Part 2 — Quote and RFQ escalation

### What it does

For every RFQ still waiting on a quote, a ladder of messages climbs from
a quiet note to the owner up to the level above the vertical head. Each
message is an in-app notification, an email where the level warrants
one, and an audit entry — all through the CRM's one notification path.

### The ladder

| Level | Fires | Reaches | Email | What it says |
|---|---|---|---|---|
| `rfq_received` | when the RFQ arrives | the owner | no | This RFQ is yours, and a quote is expected by *date*. |
| `owner_reminder` | 24 h after it arrived | the owner | no | Still no quote recorded. |
| `approaching_deadline` | 24 h before the deadline | the owner | yes | The quote is due by *date* and none is recorded. |
| `overdue` | when the deadline passes | the owner | yes | The date has passed and the customer is waiting. |
| `vertical_head` | 24 h after the deadline | the vertical head | yes | Named escalation, naming the owner it sits with. |
| `next_level` | 72 h after the deadline | the level above the head | yes | The head's reminder has not cleared it. |

**The deadline** is the date the customer was promised (`quote_by_date`
on the RFQ). Where the customer gave none, it is the service standard —
`QUOTE_SLA_DAYS` after the RFQ arrived, three days today — and the CRM
never shows an assumed date as a promise. This is the same answer the
Workbench and the quote-ageing buckets give; escalation does not hold a
second opinion about what "overdue" means.

A quote is **late at the end of the day it is due**, not at the start.
A quote sent on Friday afternoon against a Friday deadline is on time.

**Who each level reaches** is resolved by role against the employee
master at the moment it is sent, never by a name written in
configuration:

* *the owner* — the RFQ's lead driver; failing that the linked lead's
  owner; failing that the account's owner.
* *the vertical head* — the owner's named manager, failing that the
  active head of the owner's vertical. Never the owner themselves.
* *the next level* — the vertical head's own manager, failing that
  whoever holds the CRM at company level.

A level with nobody to tell sends nothing. An RFQ with no owner at all
therefore reaches only the company level — which is right, because an
unowned overdue RFQ is precisely the one nobody else will see.

### The chain ends by itself

An RFQ stops escalating the moment a quote is recorded against it, or
its status moves to Quoted, Won, Lost or Withdrawn. Nobody has to turn
anything off.

### Changing the timings — for the administrator

The timings are configuration, not code. Nobody deploys to change when
an escalation fires.

1. Open **Team & Admin → Master Data**.
2. Choose the list **Escalation Timings** (`escalation_rule`).
3. Each level is one item, whose code is the level's key from the table
   above. Its `meta` carries the timing:

   ```json
   {"hours": 24, "anchor": "deadline", "to": "vertical_head", "email": true}
   ```

   | Field | Meaning |
   |---|---|
   | `hours` | How long after the anchor the level fires. **Negative fires before it** — that is how `approaching_deadline` warns a day early. |
   | `anchor` | `received` (when the RFQ arrived) or `deadline` (when the quote is due). |
   | `to` | `owner`, `vertical_head` or `next_level`. |
   | `email` | Whether this level emails as well as notifying in the app. |

4. **Deactivating an item switches that level off.** It is not deleted,
   so turning it back on restores its timing.
5. A level with **no item**, or whose `hours` is not a number anybody
   can read, falls back to the default in the table above. A typo
   therefore delays nothing and fires nothing early; it simply has no
   effect.

There is one more item in the same list:

* `max_backlog` — **the oldest escalation still worth sending**, 720
  hours (30 days) by default. An escalation whose moment passed longer
  ago than this is not sent at all. Without it, the first sweep after a
  deployment would mail every owner about every RFQ the CRM has ever
  held. Raise it only deliberately, and expect volume when you do.

To see what is actually in force — as against what you believe you set:

```bash
.venv/bin/python scripts/escalation_sweep.py --dry-run --verbose
```

It prints every level, its hours, its anchor, whether it is on, and
whether its timing came from Master Data or from the default.

### Never twice

Each `(RFQ, level, recipient)` is written to the `escalation_log` table
once the notification has been accepted, and the unique key on that
table — not a query — is what stops a second send. The sweep runs every
fifteen minutes; without this, an RFQ one hour past its deadline would
reach its owner ninety-six times a day, and the owner would turn
notifications off, which is the only way an escalation system truly
fails.

A delivery that **failed** records nothing, so the next sweep retries
it. A mail server down for an hour costs a delay, not a missed
escalation. One record failing never stops the rest of the sweep.

### Running it

```bash
# what would be sent, sending nothing
.venv/bin/python scripts/escalation_sweep.py --dry-run --verbose

# check a timing change before it is due
.venv/bin/python scripts/escalation_sweep.py --dry-run --at '2026-10-10 09:00'

# the real thing
.venv/bin/python scripts/escalation_sweep.py --verbose
```

Exit status is 0 when the sweep ran and 1 only when it could not run at
all. A delivery that failed is reported and retried, not an error worth
waking anybody for.

#### Installing the timer

Templates live in `docs/operations/deploy/`. Check the service user and
paths against production first, as
[that folder's README](deploy/README.md) explains.

```bash
sudo cp docs/operations/deploy/procam-crm-escalation.service \
        docs/operations/deploy/procam-crm-escalation.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now procam-crm-escalation.timer
systemctl list-timers --all | grep escalation
journalctl -u procam-crm-escalation.service -n 40
```

It runs every fifteen minutes, offset from the task SLA sweep so the two
do not contend.

### Installing the table

```bash
.venv/bin/python scripts/2026_10_10_escalation.py --check   # dry run
.venv/bin/python scripts/2026_10_10_escalation.py           # apply
```

Additive: it creates `escalation_log` and seeds the Master Data list so
an administrator can see the timings on day one. Nothing is backfilled —
an escalation that was never sent cannot be recorded as sent, and
pretending otherwise would silence the first real one. The reversal
(`--down --yes`) drops the log, which means the next sweep would send
every live escalation again; the timings themselves are left alone.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| A score of 100 that looks too good | A restricted viewer sees none of the unowned records. Check the company-wide score. |
| A factor showing 0 of 0 | Nothing in its population — no lost deals, no won business yet. It deducts nothing, correctly. |
| "One or more checks could not be measured" | A Data Quality check is erroring. The Data Quality dashboard names it. Until it runs, the score is optimistic. |
| No escalations at all | Check the sweep timer is enabled, then `--dry-run --verbose`: it will show whether the levels are active and what it thinks is due. |
| An escalation nobody expected | `--dry-run --verbose` prints whether each timing came from Master Data or the default. Somebody may have edited `meta`. |
| The same escalation twice | Should be impossible. Check `escalation_log` for two rows with the same `dedupe_key`; if there are, the unique index is missing and the migration did not complete. |
