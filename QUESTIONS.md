# QUESTIONS.md

Decisions taken while building RFQ-DEMO-PARITY without stopping to
ask. Each is the safest reading of the brief; each is cheap to
reverse if the reading is wrong.

---

### 1. Dark mode is not brought across

The demo defines a full dark palette and honours
`prefers-color-scheme`. No other page in the CRM does.

**Taken:** light only. Five dark pages in a light portal is worse
than a light portal.

**To reverse:** the tokens are already in one block at the top of
the `.rfq-*` section of `static/css/crm.css`; a dark variant means
redefining that block, and then doing the same for the rest of the
CRM.

---

### 2. The CRM's own typeface stays

The brief says to use IBM Plex **if it is already served locally**.
It is not — `static/` has no fonts directory and nothing in the
repo references Plex — and the brief forbids a CDN.

**Taken:** Inter and Barlow, as every other CRM page uses.

---

### 3. "Department steps" is read as the twelve-step catalogue

The demo has no screen by that name. The nearest thing is its
"Steps that close themselves" table, and the twelve steps were
written into `task_definitions` in commit a2bf2a8 "so an
administrator can see and tune them" — with no screen to see them
on.

**Taken:** a new read-only admin page at `/admin/rfq-steps`, with
that table plus the priority windows and the Early/Delayed rule.

**If it meant something else** — most likely the per-department
conditions — that is the lead record's Conditions tab, which is
covered separately and is `same`.

---

### 4. The spend panels show a sentence, not a number

IOUs live in the TMS IOU module and the CRM–TMS link is not built,
so the CRM has nothing to read. The brief: *"Where data is missing,
show the demo's layout with 'Not set yet — <who sets it>', linked
to the readiness panel."*

**Taken:** exactly that, on the lead record's Spend tab and the
Reports spend card, linked to `/admin/crm-rules`.

---

### 5. The page-local stylesheets on four pages were deleted

The brief asks for the demo's components in `crm.css` as a scoped
`.rfq-*` section. Four pages carried their own `.ac-*`, `.ch-*`,
`.lj` and `.rp` blocks defining the same tiles, chips and tables
with slightly different values.

**Taken:** deleted, not left beside the new section. Two
definitions of a card is how the screens drifted in the first
place. `test_rfq_parity.py` asserts the new section uses only
`crm.css` tokens.

---

### 6. The top bar was added to the chain pages, and its phone
### overflow fixed only there

Four of the five pages had no `_crm_topbar.html`. The demo has a
top bar and so does the rest of the portal, so they now include
it. The shared bar does not wrap at 390 px, which gave those pages
a 146 px sideways scroll they did not have before.

**Taken:** the wrap is fixed under `.rfq-page`, a class only these
five pages carry — not in `.crm-top` itself, because the brief
says a screen whose flag is off must stay exactly as it is, and
`.crm-top` is on every page.

**Still open:** `/my-work/tasks` has the same 146 px overflow and
had it before this work (`waiting-on-me--live-phone-before.png` is
536 px wide). It is not a chain page, so it was left alone. Say
the word and the fix moves into `.crm-top` for everybody.

---

### 7. Playwright is a developer tool, not a dependency

The screenshots need a browser. `requirements.txt` is untouched:
nothing the server runs needs Chromium, and a deploy that pulls a
100 MB browser to render HTML is a deploy that breaks.

**Taken:** `scripts/parity_shots.py` documents the two commands
that install it locally, and the test suite does not import it —
the pytest tests assert structure and the stylesheet's own phone
rules, and the screenshot pairs are the record of how it looks.

---

### 8. The board shows a closed lead as closed

A lead recorded Won while a chain step is still open used to read
"With Sales · 12. Handover" under a `Won` chip.

**Taken:** the outcome wins. `rfq_board.row()` reads such a lead as
"Won · handed over". The open step is still there and still on the
reports; it is the board's summary line that changed.

---

### 9. `owner` keeps its employee code; the name is a second field

The screens needed names. Changing `owner` in place broke eleven
existing tests that match on the code, correctly — other callers
read it.

**Taken:** `owner` unchanged, `owner_name` added beside it. Same
for the chain boxes and the workload rows.

---

### 10. The name lookup is cached per request, not per process

First cut cached employee names in a module-level dict. Two things
were wrong with it: a deployment runs for days and people get
renamed, and the test suite recreates the same employee codes with
different names in one process, so one test module read another's
people.

**Taken:** the cache lives on Flask's `g`, and outside a request
there is no cache at all.

---

# CRM-LEAD-ACCOUNT-01

Decisions taken while building the account picker, 29 Oct 2026.

### 11. "Department steps" aside, this spec needed no new table

§2.5 asks for an **Owner corrections** report: date, account, old →
new, by whom, leads moved. `account_assignments` already holds the
first four; it does not hold "leads moved".

**Taken:** the correction is written as an audit event
(`account.owner_corrected`) carrying the moved lead ids, and the
report reads the audit trail. No migration, and `audit` is already
the system of record for who changed what — a second table is the
one that drifts.

---

### 12. "A person's name" means one of ours, not a guess

§2.3 lists "a person's name" among the placeholders to catch.
Deciding that a string looks like somebody's name catches
"Tata Steel" — two words, two capitals.

**Taken:** only an exact match against the active employee master.
"James Francis Xavier" is refused; "Rajesh Kumar" is accepted,
because it might be a customer. A false positive here blocks Save
on a real customer, which is worse than letting one odd name
through.

---

### 13. Save is blocked, but disposing of the lead never is

§2.4 says Save is blocked while the company is a placeholder, and
that Close as Lost, Mark duplicate and Delete – not relevant stay
allowed. Those three are separate endpoints, so they were already
exempt — the gate sits only in `PUT /api/leads/<id>`.

**Taken:** left that way, and a test asserts Close as Lost still
works on an unnamed lead. A lead can always be disposed of; it
only cannot be left half-named.

The gate also stands down when the same save carries a
`company_id`, which is how the picker links and saves in one
request.

---

### 14. The correction is written where auto-assign actually reads

§2.5.3 wants every future lead to go to the corrected owner.
`auto_assign.decide` reads `AccountVerticalOwner` **before**
`pic_emp_code`, so a correction that stopped at the account's PIC
would be undone by the next lead in a vertical that has its own
row.

**Taken:** `correct_owner` writes both, and there is a test that
seeds the stale row and proves the next decision follows the
correction.

---

### 15. Nothing moves that was not named

§2.5.2 asks for a preview before other leads move. The preview
lists only open leads — Won and Lost are never offered, because
moving a closed lead rewrites a figure somebody has already
reported on.

**Taken:** `move_lead_ids` is explicit. An empty list moves
nothing, and the test asserts that calling the endpoint without it
leaves the other leads alone.

---

### 16. The bulk select column is shared, not duplicated

§2.6 wants "select leads → Link to account" on the Leads list. The
list already had a checkbox column, added by Delete – not relevant
and shown only when that flag is on.

**Taken:** the column now appears when **either** feature is on and
each action's button appears for whoever has that feature. Two
checkbox columns side by side would have been the alternative.

---

### 17. The §5 data fix is a script, not something I ran

The BEML tender lead still needs linking to BEML LIMITED.
`scripts/link_unplaced_leads.py` does it, and it is read-only
unless given `--ids`, `--account`, `--actor` and `--apply`
together. **I have not run it against production** — it changes
lead data, which needs your approval first.

Its apply path is tested, not just its refusal: a gate that was
never built reports a clean dry run and writes nothing.

---

### 18. A fifth verdict: NO OWNER

The backfill brief names four verdicts — OBVIOUS, CHOOSE, NO
ACCOUNT, OWNER INACTIVE. A lead can also sit under an account that
has nobody at all: no vertical owner, no PIC.

**Taken:** that case gets its own verdict, `NO OWNER`, rather than
being folded into NO ACCOUNT. The two look alike in a count and
differ completely in the fix — NO ACCOUNT is corrected on the lead,
NO OWNER on the account — and folding them would have sent somebody
to the wrong screen.

---

### 19. The missing row for #12397 is the lead's, not the account's

The brief asks for the missing `AccountAssignmentHistory` row for
lead #12397. `account_assignments` records who owns an **account**;
BEML LIMITED's owner did not change on 29 October, and that change,
when it happened, was already recorded.

What the SQL `UPDATE` skipped was the **lead's** trail:
`lead_assignment_history`, the table every assignment through the
screen writes. That is the row `--history-only` writes — owner
DIR22010, actor DIR12010, reason "Manual correction 29 Oct" — plus
an audit event.

**Taken:** I wrote the row the lost trail actually belongs in and
said so here rather than writing an account row that would claim a
change that never happened. Say the word if you want an
`account_assignments` row as well.

---

### 20. A backfilled owner is told about it

`--apply` goes through `lead_assignment.assign()`, the lead sheet's
own path, so it notifies the new owner exactly as the screen does.
Backfilling a hundred leads at once therefore sends a hundred
notifications.

**Taken:** notifying is the default, because a lead somebody owns
without knowing it is the problem this is fixing. `--quiet`
suppresses it when a batch would be noise rather than news.

---

### 21. A resemblance is not a match

The first live `--unlinked` run called 26 groups OBVIOUS and about
half were wrong in the same way: "Balaji Wafers Pvt Ltd" offered
for a lead read as "Balaji Power Tech", "Hind Terminal" for
"Indo-German", "Liftec" for "Lntecc", "WHEEL INDIA LTD." for
"Rktrwheels". Each was the only answer the matcher had, so the
group counted as unambiguous.

The matcher was not wrong — `account_match` tags these
`kind='resemblance'` and says in its own docstring that a
resemblance is "no evidence at all". `lead_account.suggest()` was
dropping that tag, and the rule text cannot be told apart: a real
name match and a 62%-alike guess both read "company name".

**Taken:** `suggest()` now carries `kind` and `note`, and OBVIOUS
requires exactly one candidate **matched on evidence** — a sender
domain, a contact's address, a learned correction, a GSTIN or an
actual name match. A lone resemblance is CHOOSE, printed as
"nothing but a resemblance — probably a new account, not one of
these", and the group carries no pre-filled account id, because a
question with the answer already in it gets agreed to rather than
read.

---

### 22. A merge re-points every table, not three

FINISH-TRACKER-CRM §0 says a merge re-points "leads, contacts and
owners". There are 30 foreign keys to an account: activities, stage
history, PIC history, tags, quotes, RFQs, handovers, competitors,
projects, business cards, sender links, the account's own parent and
source columns.

**Taken:** the merge walks the metadata and re-points all of them,
reporting a count per table. Moving three and leaving 27 pointing
at a row marked inactive would have produced a worse state than the
duplicate — a customer whose quotes and history had quietly
detached from it.

Where a unique constraint makes a move impossible — both accounts
name somebody for Heavy Transport, or carry the same tag — the row
stays on the loser and is counted as "left behind". The alternative
is deleting one of two records of who served a customer, and
nothing here deletes.

---

### 23. The loser is renamed, not hidden

An inactive account with its original name is invisible in a name
search and indistinguishable from one that was simply switched off.

**Taken:** the loser becomes `"<name> (merged into #<id>)"` and
stays readable, and its old spelling is added to the survivor's
aliases. `account_match._by_name` now searches aliases as well as
names, so mail that still says the old name reaches the right
customer — without that, merging an account would make its name
unmatchable, which is the opposite of the point.

---

### 24. Which account survives is never pre-decided

**Taken:** the script suggests a survivor — most leads, then most
wins, then identifiers, then the lower id — and says in the report
that this is "evidence, not a decision". On the Admin screen the
survivor is a radio button with the suggestion pre-ticked but every
account listed with its lead count, wins and owners, so the choice
is read rather than agreed to. A merge needs `--survivor`,
`--merge`, `--actor` and `--apply` together; there is no "merge
everything it suggested".

---

### 25. TKT-0063 needed nothing built

The ongoing rule exists — `app/services/task_autoclose.py`, called
from four save paths — and so does the backlog sweep,
`scripts/task_backlog_report.py`, with `--check`, counts per rule,
and `--apply` refused without `--i-have-approval`. Both were built
on 19 October.

**Taken:** I built neither again. Tasks 1342, 1541, 1283 and 1536
are still open because the sweep has never been run, not because
it is missing. The dry-run command is in the release message.

---

### 26. The 296-lead master is a CSV beside the workbooks, not instead

`scripts/vertical_review_split.py` already turns that population
into five workbooks with dropdowns, which is the right shape for
filling one in and no use for reading the whole list or diffing it.

**Taken:** `scripts/vertical_master_csv.py` writes the same
population as one CSV under `docs/reports/`, named for the day, so
two runs a week apart leave two files whose difference is the work
somebody did. The workbooks stay. The count will not be exactly 296
— the population is read from the database, so leads reclassified
since October drop out of their own accord.

---

### 27. "Assign to" is the business unit, because that is what the
### endpoint takes

TKT-0079 asks for an "Assign to" dropdown wired to
`api_assign_crn`. That endpoint stamps an enquiry with its customer
reference and takes one argument, `bu` — the business unit the
reference is issued under. It does not assign a person.

**Taken:** the dropdown offers the business units, reads the next
reference as you change it, and assigns on the button. The units
come from the CRN register rather than a list in the code, because
`crn.PATTERN` deliberately accepts any two-to-five letters — the
business writes PWL, PLPL and PWLPL. **If you meant "assign the
enquiry to a person", say so and I will add that instead**; it is a
different endpoint and does not exist yet.

---

### 28. Decision A2 gained a flag it did not have

Everything else in the 21 October release is behind a flag.
A2 — a vertical head sees every lead of their vertical, and anyone
sees a lead they hold a task on — was unconditional.

**Taken:** `FEATURE_LEAD_ALLOWANCES`, off. It widens who can open a
lead across the whole book, which is the kind of change that should
be looked at by a pilot before everybody gets it. With the flag off
`scope.leads()` is exactly the ownership test it has always been,
and a test holds that.

---

### 29. E3, the five lost reasons, is superseded

The 21 October decision named five reasons plus Other. On 25
October — four days later — management settled on seven, and
`scripts/2026_10_25_lost_reason_mapping.py` moved every lead onto
one of them. "Not interested" survives as its own reason in the
later list and was dropped by the earlier one; folding 1,213 of
those into "No response" would lose the difference between a no and
a non-reply.

**Taken:** main's seven stand, unchanged. `LOST_REASONS_SEED` is
exactly what it was, and the test that holds it equal to the
migration's `FINAL` still passes.

---

### 30. The CRM rules editor moved to /admin/crm-settings

Both `app/admin_rules` (the pilot-readiness panel, added after this
branch) and the branch's settings editor claim `/admin/crm-rules`.
Two blueprints on one rule means whichever registers first wins and
the other silently disappears — no error, no log line.

**Taken:** the readiness panel keeps the URL its own spec points at
in four places; the editor is served at `/admin/crm-settings`. The
release note and the Email shell's link both say so.

---

### 31. Dropping the `active` column puts every row back

`scripts/2026_10_29_owner_rows_retire.py --down` cannot keep a
distinction the schema no longer has room for, so dropping the
column makes every retired owner row live again.

**Taken:** that is said in the script rather than worked around.
`--down` is for undoing the migration, not for undoing the
rollback; `--restore --apply` on the seed script is what undoes the
rollback.

---

### 32. "Assign to" is a person; the business unit is its own control

Answered 30 October: it means a person. QUESTIONS 27 read it as
the business unit because `api_assign_crn`, the endpoint the brief
named, takes only `bu`.

**Taken:** two dropdowns under two headings. **Assign to** sets who
works the enquiry, through `lead_assignment.assign()` — the one path
every screen uses, so the history row, the audit row and the
notification happen exactly as they do on the lead sheet, including
the rule that replacing an existing owner needs a reason.
**Business unit** stamps the customer reference, unchanged.

The brief says `AccountAssignmentHistory`. That table records who
owns an **account**; an enquiry is a lead, and the trail for who
holds a lead is `lead_assignment_history`. That is the row this
writes, as it did for the backfill and for #12397.

The list of people is read from the employee master, filtered to
active and to the freight-forwarding vertical. An empty list is
shown as an empty list with an explanation: falling back to
everybody would hand a freight enquiry to a warehouse clerk. Where
no vertical master is loaded, `master_vertical` answers Management
for everything, so the written name is the only evidence and the
match is on that alone.

---

### 33. Most of CRM-LEAD-FLOW-01's intake is already built

Before building §2 I read what is there. The blocked-client gate
exists in `email_ingest/single_message.py` and already refuses to
make a lead, filing the mail as "Intake — Blocked". Thread matching
exists in `lead_intake_db.find_by_thread`, by conversation id,
In-Reply-To and References, with a subject fallback. The intake
classifier already has ten classes and only one creates a lead.

**Taken:** I did not rebuild any of it. §2.1's real defect is
narrower than the brief reads: the block check was passed the name
the parser read and the sender's domain, but never the **resolved
account**, so a blocked client writing from a second domain or
under a spelling held as an account alias was not matched. That is
why Walchandnagar and Godrej kept creating leads while both were on
the register. The account is now resolved first and passed in, and
both readings of the name are checked.

What was genuinely missing from §2.3 is the **merge**: nothing
could join two leads that were already split.
`app/services/lead_merge.py` does that.

---

### 34. A subject-only match is never merged by a sweep

§2.3 gives three dedupe rules. Two are evidence — the same mail
thread, the same client reference. The third, the same subject from
the same sender, is a resemblance.

**Taken:** rules 1 and 2 mark a candidate `certain`; rule 3 marks it
a suggestion. `--apply` on the clean-up merges only the certain
ones, and the suggestion draws the "possible duplicate of #nnnn"
banner the brief asks for, where a person decides. This is the same
line the account matcher draws, for the same reason — see 21.

---

### 35. A won lead is never merged away

**Taken:** `lead_merge.blockers` refuses it in words: "A won lead is
a reported figure. Merging it away would rewrite a number somebody
has acted on." The same rule already stops the clean-up closing Won
or Lost leads.

---

### 36. §3 and §4 are not in this release

The lead pop-up (§3) and the two-step "+ New Lead" (§4) are a
rebuild of the busiest screen in the CRM — a 25-field form inside a
6,000-line template that every user is on all day.

**Taken:** everything they depend on is built and tested first —
the guided-help catalogue they show their refusals through, the
merge behind the duplicate banner, the stage simplification behind
the six-stage strip, the `sub_status` and `popup_seen_by` columns —
and the screen itself is the next release, behind
`FEATURE_LEAD_POPUP_V2`. Shipping half a lead screen behind a flag
nobody can switch on would have been worse than shipping none.

---

### 37. The All Leads list is not changed in this release

§3.4 asks for the list's columns, its default filter, a "2 similar"
link and a team dropdown of active employees only. The list is in
`templates/app.html`, the 6,000-line template this release was
asked not to touch so that flag-off users get exactly today's
screen.

**Taken:** the data behind §3.4 is built and tested —
`/api/help/people` is the active, de-duplicated employee list the
dropdown needs, and `lead_merge.candidates` is the "2 similar"
link's answer — but the list itself still renders from app.html.
Changing it is a separate, unflagged edit to a screen everybody
uses, and it belongs in its own release with its own before-and-
after. Say the word.

---

### 38. The screenshots are measured, not just taken

The RFQ screens shipped at 536px wide on a 390px phone because
nobody measured; the picture looked fine because the harness
photographed a wider viewport.

**Taken:** `scripts/lead_flow_shots.py` prints
`document.scrollWidth` at both widths beside the pictures. It found
the same defect here — the shared top bar is a 56px row that does
not wrap — and the fix is scoped to `.lf-page`, exactly as
`.rfq-page` does it, because the bar is on every screen in the CRM.

---

### 39. "Ask for help" needed a ticket register, so there is one

§5.2 says the button "creates a ticket in the existing ticket
register". There is no ticket register in this repository.

**Taken:** `crm_help_tickets` — number, who, which screen, which
lead, which guide was showing, and what they were trying to do. The
person gets `CRM-202610-00042` back instead of the Tech team's
phone number, and the Tech team gets the context without asking. If
you meant a register that lives in the TMS, point me at it and this
becomes a client for it.

---

### 40. The All Leads list, inside the flag

§3.4 is now built in `templates/app.html`, every line of it inside
`{% if feature_enabled('FEATURE_LEAD_POPUP_V2') %}`: an alternative
`<thead>`, the My/All open leads control, and one script block that
replaces `rAll` rather than editing it. Two versions of one
function are easier to tell apart than one function full of
conditions.

**Taken:** a snapshot test hashes the flag-off All Leads block and
compares it against
`tests/fixtures/all_leads_flag_off.sha256`, taken before the
change. If the page everybody is still using moves by one byte,
that test fails and says how to take a new snapshot deliberately.

Two findings fell out of building it. `opportunity_value_num` is a
`Decimal`, so dividing it by a float raised a 500 — caught by a
test, not by a user. And the open-lead filter built on
`STAGES_PIPELINE` silently drops the alias stages (Profile Sent,
Appointment, Visit Done), which are open; the list adds them back.
That is the same gap the on-hold sweep found from the other side.

---

### 41. One stage list was still two

Finding 40's alias gap was not a defect in the All Leads list. It
was in `sales_rules.open_stages()`, which returned the journey's
open stages and nothing else, so **31 places** that ask "is this
lead open" read a lead on Profile Sent, Appointment or Visit Done
as closed: the Workbench, the review funnel and its discipline,
desk and account sections, the meeting agenda, the management
view, the pipeline value KPI, the digest mail, outbound and quote
mail, the account gate, and nine backlog scripts. The three names
are only rewritten when somebody saves the lead, which is why they
are still on the rows.

**Taken:** `STAGES_OPEN` in `app.py` — the journey's open stages
plus the aliases that fold onto them — and `open_stages()` returns
it. The vocabulary a dropdown or a funnel needs is now
`sales_rules.pipeline_stages()`, the journey alone, so the two
readings of "the stage list" can no longer be mistaken for one
another. `scripts/open_stage_sweep.py` lists every site with what
it feeds and prints each count under both sets.

The question behind it: this is the second time the same shape of
bug has surfaced — one list used as a vocabulary and as a filter.
"New Opportunity" cost 156 leads in October. If the DB still holds
leads on stage names that are neither on the journey nor in
`LEGACY_STAGE_MAP`, they are invisible to both lists and the sweep
will not see them either; a one-off `SELECT stage, count(*) FROM
leads GROUP BY stage` on the server would settle it.

---

### 42. set_pilot --off never turned anything off

Building `scripts/feature_flag.py` turned this up next door.
`scripts/set_pilot.py --off` called `flags.set_flag(...)` behind a
`hasattr` guard. There is no `set_flag` — the function is
`flags.set` — so the guard swallowed it: `--off` lifted both pilot
limits and left the flag **on for everybody**, printing "limits
lifted" while doing the opposite of what somebody reaching for
`--off` wants. Every rollback line of the form `set_pilot.py
--flag X --off` that I have put in a deploy block was wrong.

**Taken:** the call is `flags.set(name, False, ...)`, there is a
test, and `scripts/feature_flag.py --code X --off --apply` is the
line to use from here on.

---

### 43. Four master verticals, not seven

Decision of 31 October 2026 (Nilesh Kumar Sinha), superseding the
seven of 18 October: the CRM has four master verticals — Heavy
Transport, Project Freight, Installation, Warehousing. Freight
Forwarding becomes an alias of Project Freight, General Transport an
alias of Heavy Transport.

**Taken:** `scripts/vertical_master_apply.py` now holds the four,
and a master vertical is never deleted — its head row is marked
inactive and an alias row is written in the same transaction, so the
resolver stops offering it and starts folding it onto the vertical it
belongs to, while the row that recorded the decision to have it is
still there to read. `All` was being *deleted* by this script; it is
retired now like the rest.

Two things fell out of it.

**An alias of an alias resolved to nothing.** Sea Freight, Air
Freight and Customs pointed at Freight Forwarding, which is now an
alias itself. `vertical_heads.master_vertical()` takes one hop and
stops, so those three would have answered "Freight Forwarding" — a
name in no head table — and every record carrying them would have
reached nobody. They are repointed at Project Freight, the script
flattens any other chain it finds, and a test holds the invariant
that no mapping target may be anything but one of the four.

**Installation's head disagrees with the 21 October decision.** This
script sets it to DIR52011 (Sethupathy Sundaram); the decision of 21
October says Gowdhaman R. I have not changed it — the question was
about the master list, not the heads, and I do not have Gowdhaman R's
employee code. Say the word and it is one line.
