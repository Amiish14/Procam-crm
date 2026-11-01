# CRM-COCKPIT-02 — going live

The order matters. The migration adds columns the running code reads;
a worker that restarts before it exists answers 500 on every lead
screen, and a back-fill that runs before the migration cannot write
anything at all.

Everything here is run on the server. Each back-fill prints what it
would do first, and every one of them can be undone with one command.

---

## 1. The database, before the code

```bash
cd /var/www/procam-crm && git pull
.venv/bin/python scripts/2026_11_01_cockpit02.py --check
.venv/bin/python scripts/2026_11_01_cockpit02.py
```

Fifteen nullable columns on `leads`, two new tables
(`lead_quote_due_history`, `crm_bulk_change_log`) and three indexes.
Nothing existing is touched. `--down --yes` reverses it, and dropping
`crm_bulk_change_log` is what makes every back-fill below permanent —
so leave it in place until the release has settled.

## 2. The code

```bash
sudo systemctl restart procam-crm
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8002/health
```

No feature switch (§0.1): this is live for everybody the moment the
workers come back. The nine switches turned on in October are
untouched.

## 3. The back-fills

Each one twice: `--check` reads and prints, `--apply` writes. Read the
first before running the second — the counts are the evidence for the
mail §0.6 asks for.

```bash
# §2.8 — the submission deadline, off the enquiry mail
.venv/bin/python scripts/quote_due_backfill.py --check
.venv/bin/python scripts/quote_due_backfill.py --apply

# §5.6 — the portal numbers, then fold the duplicate sets
.venv/bin/python scripts/portal_dedupe.py --check
.venv/bin/python scripts/portal_dedupe.py --apply

# §4.1 — when anything last happened, from mails and the audit trail
.venv/bin/python scripts/last_activity_backfill.py --check
.venv/bin/python scripts/last_activity_backfill.py --apply

# §5.1 / §5.2 — the lead fields the mail already said
.venv/bin/python scripts/autofill_backfill.py --check
.venv/bin/python scripts/autofill_backfill.py --apply

# §3.4 — a next date on every open lead
.venv/bin/python scripts/followup_backfill.py --check
.venv/bin/python scripts/followup_backfill.py --apply

# §4.5 — open leads with no owner
.venv/bin/python scripts/unowned_sweep.py --check
.venv/bin/python scripts/unowned_sweep.py --apply
```

Order matters in two places. The deadline back-fill runs before the
follow-up one, because at RFQ Generated the follow-up is a day before
the deadline and a follow-up set first would be set from the wrong
rule. The portal numbers go in before `close_pending.py`, because
§6.7's five auctions are found by their number.

## 4. The pending items (§6)

```bash
.venv/bin/python scripts/2026_11_01_close_pending.py --check
.venv/bin/python scripts/2026_11_01_close_pending.py --apply
```

Read the check output before applying. It prints a section called
**THINGS THIS RUN CANNOT DO**, and everything in it needs a person:

* a lead named by number that is not in the database;
* an owner name that matches nobody, or two people;
* **the two leads §6.1 says are not flagged blocked.** The script says
  whether the register matches each of them now. Where it does not,
  closing the lead does not fix anything — the next enquiry from the
  same client comes straight through. Add the name as an alias, or
  link the lead's account to the blocked entry, on
  `/CRM/admin/restrictions`;
* **Godrej**, which this script does not block. That decision names
  which Godrej companies it covers, and
  `scripts/2026_10_21_block_godrej.py` asks:

  ```bash
  .venv/bin/python scripts/2026_10_21_block_godrej.py
  .venv/bin/python scripts/2026_10_21_block_godrej.py \
      --apply --ids <the ones management means> --approver DIR12010
  ```

Items 3 and 4 of §6 have their own scripts and are not re-run here:

```bash
.venv/bin/python scripts/crm2_backfill_stage_entered.py --check   # §6.3
.venv/bin/python scripts/vertical_master_apply.py --check         # §6.4
```

## 5. The two timers

```bash
cd docs/operations/deploy
sudo cp procam-crm-quote-due.service procam-crm-quote-due.timer \
        procam-crm-unowned.service procam-crm-unowned.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now procam-crm-quote-due.timer
sudo systemctl enable --now procam-crm-unowned.timer
systemctl list-timers --all | grep -E 'quote-due|unowned'
```

The quote-due sweep runs hourly between 08:30 and 20:30 IST. A
deadline is a time of day: a daily pass would tell somebody at 09:00
that an auction closed at 15:15 yesterday.

## 6. The acceptance checks (§9)

```bash
.venv/bin/python scripts/cockpit02_acceptance.py --as DIR12010
```

All ten, printed PASS / FAIL / SKIP. A **SKIP is not a pass** — it
names the back-fill that has not been run. The four checks that need a
record to act on create their own, marked
`ZZ-ACCEPTANCE-CRM-COCKPIT-02`, and close it as a Duplicate when they
finish; the run says how many it left open, which should be 0.

Run it again for the other two people §9 names:

```bash
.venv/bin/python scripts/cockpit02_acceptance.py --as EMP372011
.venv/bin/python scripts/cockpit02_acceptance.py --as EMP572012
```

## Undoing any of it

Every back-fill writes a batch to `crm_bulk_change_log`:

```bash
.venv/bin/python scripts/undo_batch.py --list
.venv/bin/python scripts/undo_batch.py --batch <name> --check
.venv/bin/python scripts/undo_batch.py --batch <name> --apply
```

An undo refuses to overwrite a field somebody has edited since, and
refuses to run twice. It does not touch a lead that was closed by
`close_pending.py` through the register's own path — those are
reopened on `/CRM/admin/restrictions` by lifting the block, which is
a decision, not a rollback.

## What this release does not change

Feature switches, vertical master data, permissions, and the nine
switches turned on in October. No lead is deleted anywhere in it.
