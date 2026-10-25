# R1 — Account first (`FEATURE_ACCOUNT_FIRST`)

CRM-ACCOUNT-FIRST-01. Off by default. With the flag off every
screen, route and count behaves exactly as it does today, which is
what makes the rollback a switch rather than a revert.

## What it changes, on

- A lead with **no account**, or whose account has **nobody named
  for its vertical**, is held. It cannot be moved past New,
  registered as an RFQ or quoted. Four doors, one guard
  (`app/services/account_gate.py`), called from all of them —
  `value_gate` exists in one place and is called from five for the
  same reason.
- **Lost, On Hold and Not Interested stay open.** A lead that turns
  out to be nothing must be closeable without first inventing a
  customer for it, or the queue becomes a trap and the way out is
  to click something untrue.
- **The mail intake now saves `lead.company_id`.** It always
  resolved an account; it used it to pick an owner and then threw it
  away. It is written only where linking needs no judgement — a
  learned sender, or exactly one exact-rule hit with nothing
  disagreeing. Everything else goes to Account check, which is the
  point of the queue.
- **Account check** at `/accounts/check`: the queue, each lead with
  its candidates and the rule that matched each one, the owner for
  that vertical, and a blocked flag. Confirming links the lead,
  names the owner, and teaches the matcher the sender — that last
  step is what makes the queue shrink instead of refill.
- Who may clear it: the **vertical head for their own vertical**, or
  an admin (D6). Head-ness is read from the `vertical_head` table,
  not from a role (D12) — a head whose role was never updated is
  still the head.
- A manual lead naming an account goes to that account's owner for
  the vertical; the creator is recorded and told where it went.
- Cockpit tile **Accounts to confirm**.

## One stage list (decision 5)

There were three, and they disagreed. `stage_map.JOURNEY` drew the
lead page; `STAGES_PIPELINE` decided what `open_leads()` counts;
`LEGACY_STAGE_MAP` rewrote stage names on save. **"New Opportunity"
was a journey stage in the first, absent from the second and legacy
in the third** — so 156 live leads sat on a stage the CRM's own
definition of "open" excluded. They were invisible to the Workbench,
the cockpit and every value report, and saving one moved it
backwards to Call Done.

`JOURNEY` is now the source; `STAGES_PIPELINE`, `STAGE_NEXT` and the
alias half of `LEGACY_STAGE_MAP` all derive from it.

**This is a visible change with the flag off** — it is not gated,
because a stage list cannot be half-applied. Expect open-lead counts
to rise by about 156 the moment it deploys.

## Deploy

    cd /var/www/procam-crm && git pull
    .venv/bin/python scripts/2026_10_25_account_first.py --check
    .venv/bin/python scripts/2026_10_25_account_first.py
    .venv/bin/python scripts/2026_10_25_one_stage_list.py --check
    .venv/bin/python scripts/2026_10_25_one_stage_list.py --apply
    sudo systemctl restart procam-crm

Then, **before the flag**, and stopping for approval on the dry run:

    .venv/bin/python scripts/seed_account_vertical_owners.py --check \
        --list-unresolved
    .venv/bin/python scripts/seed_account_vertical_owners.py --apply

(That script already existed, from CRM-AUTO-ASSIGN-01. It gained
`--list-unresolved`; its rules — the replacement map for people who
have left, vertical aliases, and Management not counting as a
service vertical — are unchanged.)

659 of 1,053 accounts have no owner row and Warehousing has four in
the whole database. Switching the flag on before seeding opens a
queue nobody can clear in a week, and the obvious response to that
queue is to click through it — which is how bad ownership data gets
made.

Only then:

    flag.FEATURE_ACCOUNT_FIRST → on for DIR12010, then Heavy
    Transport, then everyone.

## Rollback

Flag off. The two migrations have `--down --yes`; the seeding script
removes exactly the rows it wrote (`source='seeded'`) and leaves
manual decisions alone. No data is deleted by any of it.

## Still to do

- §3.4's `account_owner_confirmation` table exists and is enforced
  unique per account × vertical, but nothing writes to it yet — the
  morning digest that asks the question is R1's remaining half.
- §6.2's activity kinds (decision 7) belong to R4 and are not in
  this release.
