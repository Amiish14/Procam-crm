# R2 — the RFQ chain (`FEATURE_RFQ_CHAIN`)

CRM-RFQ-CHAIN-01. Off by default; every route 404s while it is.

## What it is

The SCM team's twelve steps, running on the existing task engine
rather than beside it. A step is a `TaskInstance` with a
`step_no`; the engine that already carries 3,693 instances
carries these too.

R2 adds the four things that engine had no word for:

| | |
|---|---|
| `step_no` | which of the twelve this is — null on all 3,693 existing rows, which is true |
| `paused_seconds` | time on hold, never counted as work |
| `result` | Early / On time / Delayed, computed at close and then frozen |
| `delay_reason` / `good_practice_reason` | asked only on an exception |

## The two rules worth stating

**Steps close on events.** The cost sheet saving closes step 6,
approval closes 7, the quote going out closes 8, the PO closes
11. Only Screening, Technical Evaluation and Survey take a
Confirm click, because only those three have no event of their
own — and there is deliberately no route that closes the others.
A chain whose steps can all be ticked records what people say
happened rather than what did.

**A reason is asked only on an exception.** Every step asking
"why" trains people to pick the first option. Late asks for a
delay reason, unusually early asks for a good-practice one, and
on time asks for nothing.

## Timing

Standard time = the priority's window × the step's share, both
settings. Critical 2 days, High 4, Medium 7, Low 10 (D3).

Colours: red overdue, orange due within min(24h, half the step),
green otherwise, purple held, blue waiting on the customer. The
orange rule is a minimum rather than a flat 24 hours because a
Critical registration step is 2.4 hours long and a 24-hour
warning would never fire on it.

**Steps 9–11 have no standard time, on purpose.** 9 and 10 run on
a response clock per revision; 11 waits on the customer's own
decision. Colouring that red would blame the sales team for the
customer's board meeting.

**Held time is never counted.** On reactivation the held seconds
go to `paused_seconds` *and* every open due date moves by the
same amount — otherwise a step comes back already overdue for a
pause nobody caused, and the hold button becomes the thing people
avoid.

## Deploy

    cd /var/www/procam-crm && git pull
    .venv/bin/python scripts/2026_10_26_rfq_chain.py --check
    .venv/bin/python scripts/2026_10_26_rfq_chain.py
    .venv/bin/python scripts/seed_rfq_chain.py --apply
    sudo systemctl restart procam-crm

Then, per release:

    .venv/bin/python scripts/set_pilot.py --flag FEATURE_RFQ_CHAIN \
        --people DIR12010 --apply

**Nothing is backfilled.** Production holds 0 RFQs, so there is no
chain to reconstruct — and inventing step timestamps for work
that happened on a spreadsheet would put made-up numbers in the
one report this release exists to make trustworthy.

## Settings to enter on /admin/crm-rules

- `rfq_coordinator` — the employee code who registers RFQs (D4).
  Without it, step 2 opens owned by nobody.
- `rfq_scm_by_vertical` / `rfq_ops_by_vertical` — `{vertical: code}`.
  Without them, steps 3–6 open unowned and sit in a pool.
- Optional overrides: `rfq_priority_days`, `rfq_step_shares`,
  `rfq_response_hours`, `rfq_early_fraction`.

## Rollback

Flag off. The migration has `--down --yes`; no existing row is
rewritten by any part of it.

## Not in R2

Steps 6–8 and 11–12 have their columns and their close path, but
the *events* that fire them belong to R3 (cost sheet, approval,
quote sent) and to the existing Won handover. Until R3 they can
be closed only by the code calling `rfq_chain.close_step(...)`
with the right event, which nothing does yet — so in practice a
chain today runs steps 1–5 and then waits. That is deliberate:
wiring step 8 to "quote sent" before R3 defines what sending
means would be a guess.
