# Release — CRM-AUTO-ASSIGN-01: matching first, then ownership

18 October 2026. Behind `FEATURE_AUTO_ASSIGN`, **default off**.
Four new tables, all additive. No lead, account or contact is
written by the migration.

## Why matching comes first

Stage A measured it. On 30 days of real mail, **356 of 740 senders
resolved to an account — 48 per cent**. Two rules did all the work:

```
account email domain   294
contact email           62
account website domain    0
GSTIN                     0
company name              0
```

Three of the five rules in `resolve_account` had never fired once.
Meanwhile 94 per cent of new leads already got an owner, so
assignment was not the problem. Every unmatched mail is a lead whose
owner is a fallback rather than somebody who knows the customer —
routing those well is worth less than not having them.

## What changed in matching

`app/services/account_match.py`, seven rules in a deliberate order:
what a person told us, then what we know exactly, then what we infer.

**Free mail is excluded from every domain rule.** Two customers on
`gmail.com` are two customers, and an account listing `@gmail.com`
in its domains would collect the entire internet. For those senders
only the exact contact address counts. 30 domains on the list.

**The website rule compares domains, not substrings.** It used
`website LIKE '%adani%'`, which matches `adani.com`,
`adaniports.com` and `notadani.com` alike — untrustworthy, so never
reached. It now parses the domain out of whatever somebody typed
(`https://www.x.com/about` → `x.com`) and matches exactly or on a
subdomain.

**GSTIN from the body or the signature.** An exact,
government-issued identifier sitting in a great many Indian business
mails.

**Company name, normalised, and only when unambiguous.** Pvt, Ltd,
Limited, punctuation — and, for this rule only, trailing geography:
India, Asia, APAC. Two accounts that normalise alike match neither,
because a merge is a decision somebody makes.

*Scoped deliberately:* `company_match.norm` is shared with account
de-duplication and the TMS integration's find-or-create. Widening it
there would change which accounts those merge. `strict_norm` lives
in the matcher and is tried only after the conservative pass fails.

**It learns from corrections.** When a person reassigns a lead that
is linked to an account, the sender is remembered — the address
always, and the domain unless it is free mail — so the next mail
from that company matches with nobody doing anything. Only from a
person: `actor in ('system', 'auto-assign')` is skipped, because a
guess that teaches itself compounds. Every learned link is audited.

## Ownership

An account can now have a **different owner per vertical**
(`account_vertical_owner`). Godrej's warehousing and Godrej's heavy
transport are not the same salesperson and one `pic_emp_code` cannot
say so.

The rule, in order:

1. the account's owner **for this vertical**
2. the account's general owner
3. the head of the lead's vertical — *flagged for confirmation*
4. the backstop (DIR12010, a setting) — *flagged for confirmation*

At every step, an owner who has left is replaced from
`owner_replacement`, a table an administrator maintains. The chain is
followed — if A was replaced by B and B has since left, B's row
applies — with a guard against a cycle.

**A lead is never left without an owner and never left with an
inactive one.** Step 4 is the floor.

### Unmatched new customers — changed from the brief

The brief asked for "Owner to confirm" as a pending state with a
per-lead mail. This assigns the head **as the real owner
immediately** and records that nobody has confirmed it. The
difference matters: a lead with a placeholder owner is a lead
nothing chases.

Confirmation is **one morning digest per head**, not one mail per
lead. On last month's volume a per-lead mail would have been ~380
messages in 30 days, nearly all to two people. Unanswered after 48
hours, the lead appears on the backstop's digest.

Whatever is unconfirmed also shows on the head's Workbench under
**Owner to confirm**.

### Every automatic assignment is audited

`lead.auto_assigned`, carrying the rule that chose, the account and
sender it matched, and the previous owner.

## What this does not do

- **It does not touch the 97 leads whose owner differs from their
  account's owner.** Mostly deliberate human corrections, left alone
  by decision.
- **It does not write account owners.** The 28 accounts with none are
  listed with a suggestion for a person to approve.
- **It does not backfill.** The seeding and the unowned-lead fix are
  separate scripts, dry-run by default, applied only on instruction.

## The morning digest

`scripts/send_owner_digests.py`, to run beside the 08:30 brief.

One mail per head, listing every lead a rule gave them, each with
**Yes, mine** and **Give it to somebody else**. Not one mail per
lead: at last month's volume that would have been ~380 messages in
thirty days, nearly all to two people, and the second hundred would
have gone unread.

Anything unanswered after **48 hours** appears on the backstop's own
digest. It is *not* reassigned — a head who is on leave should come
back to their work, not to somebody else having taken it — only
raised where it will be seen.

Both buttons land on `/owner-confirm`, which lists everything
waiting on you. Confirming is a POST with a CSRF token: a link that
changes data on GET is a link a mail scanner can press. Answering
somebody else's is 403 unless you are an administrator.

## Replacements — `Admin → Replacements`

`admin.employees`, not `admin.email`: this is about who works here,
and the person who maintains the employee master is the person who
knows somebody has left.

Only people who have left are offered on the left, only active
people on the right, and an inactive successor is refused — it would
move the problem rather than solve it. Every change is audited.

## What the dry runs found

Worth recording, because two of them contradict the brief:

**The match rate did not move — 48% before, 48% after.** The three
"dead" rules were dead for want of data: **7 of 1,017 accounts have
a website and none has a GSTIN**. The code was fine; there was
nothing to match against.

**And 48% was the wrong number to chase.** Of 384 unmapped senders,
**290 were skipped anyway** — newsletters, `uber.com`,
`luxuryescapes.com`, `projectcargo-weekly.com`. The real gap is
**94 messages across 39 domains** that became leads and matched
nothing: `pandacs.com.cn` (14), `indo-tech.com` (8), `andritz.com`
(7). Adding those to their accounts' `email_domains` does more than
any rule — hence `domain_to_account_report.py` and its applier.

**The 28 accounts with no owner need nothing.** Every one is a
`[merged into #N]` tombstone with zero leads.

**Open leads with an inactive owner: zero.** The replacement map has
nothing to do today and is insurance. Four of its nine names are not
on the employee master at all and were refused rather than guessed:
Suketu Bhupesh Thakkar, Manas Ganguly, Setu Prassan, Laxmi Yadav.

## Deploy

```
cd /var/www/procam-crm
git pull
.venv/bin/python scripts/2026_10_18_auto_assign.py --check
.venv/bin/python scripts/2026_10_18_auto_assign.py
sudo systemctl restart procam-crm
sleep 10
curl -sS -o /dev/null -w '%{http_code}\n' https://procamlogitech.com/CRM/login
```

The restart is needed: four new models and a new Workbench source.
The flag stays off, so nothing behaves differently yet.

Then, read-only, in this order:

```
.venv/bin/python scripts/match_rate_report.py --days 30
.venv/bin/python scripts/seed_account_vertical_owners.py --check
.venv/bin/python scripts/fix_unowned_leads.py --check
```

Nothing applies until you say so.

## Rollback ladder

**1 — Turn the flag off.** `Admin → Email → Switches`. Ten seconds,
every worker, no restart. The matcher falls back to
`resolve_account`, the rules stop, confirmations stop being created.
This is the whole rollback for the behaviour.

**2 — A learned link is wrong.** Delete that row. The matcher falls
through to the next rule.

```sql
DELETE FROM sender_account_link WHERE value = 'buyer@example.com';
```

**3 — A seeded account-vertical owner is wrong.** Delete the row, or
correct it on the account page; rows a person set are marked
`manual` and the seeder never overwrites them.

**4 — Take the code off.** Revert the commit and restart. The four
tables stay and are simply unread; `sender_account_link` holds what
people have taught the matcher and is worth keeping even then.

## Tests

`tests/test_auto_assign.py` — 38, one per rule plus the matching
behaviour: free mail, substring-vs-domain, normalised names,
ambiguity, learning, replacement chains and cycles, the backstop,
the audit row, confirmations, and the Workbench tile.
