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

**Installation's head disagrees with the 21 October decision.**
Answered, and it was worse than a disagreement. Gowdhaman
Rajakrishnan is EMP472012, and
`scripts/2026_10_25_installation_head.py` set the live row to him on
25 October (D12). This script's `HEADS` still said DIR52011
(Sethupathy Sundaram) — and its apply path *writes* `head_emp_code`
for every vertical in that map, so running it would have reverted the
25 October correction and handed the Installation Account check queue
back to the wrong person, silently, because the report says nothing
about a head it did not mean to change. Corrected to EMP472012, with
three tests: the code itself, that every head in the map is an
employee on file, and that an --apply leaves Gowdhaman as the head.

---

### 44. CRM-COCKPIT-LINKS-01 — built, switched on, unreachable

Four screens existed and nothing in the CRM led to any of them: the
Cockpit, the lead journey, the RFQ chain board and the RFQ reports.
Typing the URL worked; that was the only way in.

**Taken:** the Cockpit is the landing page and first in both menus,
the logo returns to it, the lead pop-up carries a Journey button and
a read-only stage line, every lead list row carries a journey icon,
a Cockpit row opens that lead's journey, and the chain board and RFQ
reports are in both menus.

Three things worth knowing.

**Every link is gated per person, not on the global switch.** Each
of those routes answers 404 for anybody its pilot does not cover, so
a menu built on `feature()` would offer links that do not open — the
same bug this ticket is about, pointing the other way. The new
template global is `feature_for()`, and `/` falls back to the old
landing pages for anybody the cockpit flag does not reach.

**`default_landing_page` has never been read.** `index()` reaches it
with `getattr(emp, 'default_landing_page', None)`, and the column is
not mapped on the `Employee` model — only added to the table by
`scripts/2026_09_04_crm_foundation.py`. So the getattr has always
returned None and the preference has never once decided anything.
The cockpit landing does not depend on it either way; if you want
the per-person preference to work, that is a separate half-hour.

**I could not do three things in the prompt.** I cannot read the
server log, deploy, or mail you anything, and I have no access to
the Build Prompt Tracker to move the item to "live". The screenshots
are in `docs/reference/cockpit_links/`, the deploy block is in the
reply, and the back-fill dry run has to be run on the server —
nothing here has touched live data.

---

### 45. CRM-VERTICAL-SAVE-500-01 — the cache, not the save

Reproduced locally, so the traceback is in hand without the server
log: `DetachedInstanceError` in `vertical_heads.gaps()`, raised on
the **redirect target**, which is why the change was saved. It had
been committed a request earlier.

`vertical_heads._load()` cached the `VerticalHead` **ORM instances**
for 30 seconds. The save commits — expiring every attribute on them
— then clears the cache, and then `sync_employee_flags()` calls
`_load()` again and refills it with instances from that same
request's session. The request ends, Flask-SQLAlchemy removes the
session, and those instances are now detached as well as expired.
The redirect lands on the screen, `gaps()` reads
`row.head_emp_code`, and SQLAlchemy raises.

**Taken:** the caches hold plain values — `Head`, `Stored`, `Live`
named tuples with the ORM's own field names, so no reader changed.
Every save on these screens is now Post/Redirect/Get with its side
effects after the commit, each one caught: a side effect that fails
shows "Saved, with one part incomplete" and names the step, never a
500.

Two other things the same shape, found by looking:

**`client_restrictions` cached ORM rows too, and that one failed
open.** `check()` catches everything and answers "not restricted",
so for up to 30 seconds after any commit a blocked client could have
been let through, with nothing on screen and only a logged
"restriction check failed" behind it. There is no way to tell from
here whether it ever happened; the audit trail records leads
created, not checks that silently passed. If you want, I can look
for leads created against a blocked name since the gate went on.

**Three screens were writing audit rows that were never saved.**
`audit.record()` defaults to `commit=False`, and on the alias save
and both replacement saves nothing afterwards committed — so every
alias change and every replacement change since those screens were
built went unrecorded, on the one screen whose whole subject is
where a departed person's leads went. They commit now.

`switches_screen` is the one save I did not convert: nine switches
went live this morning, and restructuring the screen that writes
them, on the same day, to fix a refresh-resubmits problem is not a
trade I would make without you saying so.

## 46 — did the fail-open gate let a blocked client through?

Unknown, and the audit trail cannot answer it: `record_attempt` logs
refusals, not checks that silently passed. While the register was
cached as ORM instances, `check()` answered "not restricted" for up to
thirty seconds after any commit, with only a logged "restriction check
failed" behind it.

`scripts/blocked_client_leaks.py` is the read-only audit that can
answer it: every lead created since the gate went live, matched
against the register as it now works, split into the ones a blocked
entry would have refused outright, the ones already closed as
"Client blocked by management", and the near misses that only ever
warned. Nothing is written and nothing is committed; a test holds that
by making `db.session.commit` raise.

Open for Nilesh: run it on the server and decide, per lead, whether to
close it on the register screen or lift the block.

## 47 — `FEATURE_BLOCKED_CLIENT_GATE` is read nowhere

The switch exists in `flags.py` and is named in the CRM-LEAD-FLOW-01
prompt as the gate's flag. Nothing reads it: `grep -rn
BLOCKED_CLIENT_GATE` over the whole tree finds the declaration and the
spec, and no call site. The register is enforced unconditionally
instead — `email_ingest/single_message.py` checks it on every inbound
message, and the lead, import, RFQ, quote, card and API paths all call
`restrictions.check()` with no flag around it.

Which way round that should be fixed is Nilesh's decision: wiring the
flag in gives a way to stand the gate down in an emergency and also a
way to switch enforcement off by accident; deleting the flag makes the
code honest about a control that is always on. Enforcement works
either way today, so nothing is urgent.

## 48 — lead #12490 matched a blocked client on 31 Oct

Found by `scripts/blocked_client_leaks.py` on the server: one lead,
Walchandnagar Industries Limited, 31 Oct 10:13 IST, `source=email`,
owner EMP3592024, matched on the exact name. The script now prints the
two things that tell a gate failure apart from an ordinary sequence —
whether the entry was already in force when the lead was created, and
how many of the same client's mails the gate stopped at intake in the
same window.

Still only answerable from the server log: whether
`restriction check failed` appears near 2026-10-31 04:43 UTC. That line
is what the fail-open cache left behind, and it is the difference
between "the gate blinked for thirty seconds" and "the intake saw a
different name than the lead ended up with".

## 49 — CRM-COCKPIT-02, and the four things in it I could not finish

Built and tested; the deploy, the back-fills and the §9 run are
Nilesh's, because I have no access to the server.

**The way of working changed.** §0 of this prompt replaces it: deploy
straight to production, no feature switch, no pilot, data changes
applied without a dry-run approval step. The code follows it — nothing
in this release is behind a flag. The dry runs stayed anyway, as
`--check` on every script, because "no approval step" is about not
waiting for Nilesh, not about writing to 2,391 leads unseen. Every
bulk change also writes to `crm_bulk_change_log`, which §8 asks for.

**IST is stored as UTC.** §2.1 asks for the due date in IST. Every
other datetime in this database is naive UTC, and a column holding IST
would be compared against `utcnow()` by the first reader who did not
know — five and a half hours wrong, in the direction that says a
closed auction is still open. So it parses IST, stores UTC, and
renders IST. A screenshot caught the one place I got it wrong (the
form box showed the UTC string under an IST heading, and saving it
unchanged moved the deadline back five and a half hours); four tests
now hold the round trip.

**"Reconcile with the ageing chart" (§4.2) is not an equality.** The
No-activity tiles measure silence since anything last happened; the
ageing chart measures how long a lead has sat in its stage. A lead can
be three days into Quoted after a month of silence, so the two
numbers are not the same number and making them equal would mean
breaking one of them. What they now share is a population and a
source: both read open leads from the same board build, and the tiles
nest (30D ⊆ 15D ⊆ 7D), which is what acceptance check 6 holds.

**Four things need a person, and are reported rather than guessed:**

1. §6.1's two leads that the register does not match. Closing them
   does not fix it — the next enquiry from that client comes straight
   through. The script says which ones and what to add.
2. Godrej. §6.2 says "apply the block (already decided)", but Godrej
   is a group of separate companies and the existing script asks which
   of them the decision covers. I have not chosen for management.
3. §6.6's four owners are given by name. Resolved against the employee
   master at run time, and refused where a name matches nobody or two
   people: a wrong employee code in an owner column is silent, and the
   lead simply stops appearing on anybody's list.
4. §6.5's four lead numbers are from production. A number that is not
   in the database is reported, not skipped.

**What I could not do at all:** deploy, run anything on production,
mail Nilesh, or move the item in the Build Prompt Tracker.
`docs/operations/COCKPIT02_GO_LIVE.md` is the runbook, in order, with
the undo for every step.

## 50 — the Installation head, decided twice in nine days

PROCAM-DECISIONS-02NOV: "Installation vertical head = Sethupathy
Sundaram, in CRM and TMS. Ignore the earlier note to set Gowdhaman R
as Installation head."

Worth recording because the first decision was already applied. On
24 October D12 said the head was Gowdhaman Rajakrishnan (EMP472012);
`scripts/2026_10_25_installation_head.py` wrote that to
`vertical_head` the next day, and on 31 October I corrected
`vertical_master_apply.HEADS` to match it — after finding that its
apply path *writes* `head_emp_code` for every vertical in the map, so
the stale DIR52011 there would have reverted the correction silently.

The 2 November decision puts it back. The same hazard now points the
other way, so three things changed together rather than one:

* `HEADS['Installation']` is DIR52011 again, with both dates in the
  comment, because a value that has been wrong in both directions is
  not safe to leave uncommented;
* `scripts/2026_11_02_installation_head.py` moves the live row,
  reversibly, and refuses if DIR52011 is not an employee on file;
* `scripts/2026_10_25_installation_head.py` is **guarded shut** — it
  raises before argument parsing, so no form of the command runs,
  `--down` included. Its text is kept: the row it wrote is what the
  new script has to find, and a one-line script that still runs is a
  script somebody runs.

Gowdhaman keeps his role, the six Installation accounts he owns, and
the Nippon Express ownership Module 2 gives him. Heading a vertical
and serving customers in it are different jobs.

## 51 — the mailbox check was reporting the right answer as a conflict

Rule 7 of the pack ("CRM outgoing mail goes from tms@ with the [CRM]
subject prefix. No crm@ mailbox. Remove every 'waiting for crm@' hold")
turned out to need no code change: the sender is already tms@, the
`[CRM]` prefix is applied in `app/services/mailer.py` from
`mail_subject_prefix`, and no path waits on a crm@ mailbox.

What it did find is a reporting bug. `scripts/crm_figures.py` checked
this decision against the **12 October** version of it, which gave the
CRM its own crm@ mailbox:

    state='CONFLICT' if 'tms@' in sender else 'check'

So the report flagged the correct sender as a conflict, and would have
gone on doing so for as long as the system stayed right. It now reads
the 20 October decision and needs both halves — sending as tms@ *and*
the prefix being set, because one address with nothing to tell the two
applications apart is the problem the prefix exists to solve.

A check written against a superseded decision does not fail loudly. It
reports a conflict that is really a compliance, and the person reading
it goes looking for a bug that is not there.

## 52 — "today" was the server's date, not the users'

Found by the test suite, not by reading the code: the first run of it
after 18:30 UTC failed on a one-line assertion.

    assert got.phone_call_date == date.today()
    E  assert datetime.date(2026, 11, 2) == datetime.date(2026, 11, 1)

The call-log route stamped `phone_call_date` from `datetime.utcnow()`
and the test compared against the server's local date. Both were
wrong in the same way, which is why they had agreed until then: the
database keeps naive UTC, so between 18:30 and midnight UTC it is
already tomorrow in IST. A call logged at 01:00 IST on Tuesday was
stamped Monday, and `followup.propose()` counted "today + 2" from a
day that had already ended in Mumbai — for five and a half hours of
every day, a follow-up landed a day early and the call was on the
wrong day's report.

`quote_due.today_ist()` is now the one answer to "what day is it
where the users are", next to the `to_ist()` the deadline card
already used, and the call log and the follow-up proposal both read
it. Three tests hold it, including the 18:29/18:30 turnover.

Worth writing down because of how it was found. CRM-COCKPIT-02 fixed
the same confusion in the deadline card — a UTC moment under an IST
heading — and I still wrote `when.date()` in the route next door.
Dates and datetimes do not look like the same bug, and one timezone
used consistently for storage does not make the dates right.

## 53 — "remove every DRAFT flag" is not the same as deleting the word

Module 1 says: "Publish it as v1 and remove every DRAFT flag (CRM2
STC/SCC library and RFQ Chain term sets)."

The six October term sets say `DRAFT — replace before issuing.` in
their first line, and the prose under it is a placeholder that says
what a clause is *for* rather than what Procam's contract says:

    DRAFT — replace before issuing.
    1. Storage is charged per the unit and period stated, part
       periods counting as whole.

Deleting the first line would have satisfied the instruction exactly
and left placeholder wording looking approved, on quotes going to
clients. So `scripts/2026_11_02_terms_master.py` replaces each
annexure's wording instead — the company-wide clauses move up into
the master, where the decision puts them, and the annexure keeps only
what genuinely differs by vertical.

Two things that follow from doing it that way:

**ODC is not a vertical.** The decision lists "ODC: survey and permit
lead time" beside three verticals. ODC work is quoted inside Heavy
Transport, so those clauses are in the Heavy Transport annexure.

**One vertical keeps its DRAFT.** Sea Freight has a term set and
nobody has given me clauses for it, so it is left exactly as it is,
marker included, and both `--check` and the Admin screen say so. A
vertical whose contract language nobody has written is not one I can
write for, and an unmarked placeholder is the worse of the two
failures.

## 54 — the handover pack omitted fields rather than inventing them

Module 1 ends: "replace the invented handover fields (9 in
FF-COMMS-01, 21 in CRM2) with the fields the TMS Job / FF Job create
screens actually require, read from the TMS code. Invent no fields."

I read the TMS source rather than correcting a list by hand, and the
finding was the other way round from the brief. `scripts/handover_field_audit.py`
reports **0 invented fields** — every field the pack sends has a place
at the far end. What it found instead:

* **`po_currency`, not `currency`.** The TMS ClientPO column is
  `po_currency`. The pack said `currency`, so the value was dropped
  without a word and the order read INR — by fallback, which is right
  by accident for INR and wrong for every other currency we quote.
* **Four cargo columns declared and never filled.** `cargo_weight_kg`
  and the three sides were in `PART_B_JOB` and nothing wrote them, so
  the weight and dimensions CRM-COCKPIT-02 §5 had just started
  capturing went nowhere and the TMS job was typed from the enquiry
  mail a second time. `split_dimensions()` now splits the one string a
  person types into the three columns and the unit the TMS keeps, and
  **refuses rather than guesses** — an ODC job is priced on its
  dimensions, and 7.2 in the wrong unit is worse than a blank.
* **`cha_name` and `scope`** are read by the TMS and not sent. Both
  are reported, neither is filled: I have no CRM source for a CHA's
  name, and inventing one is the thing the instruction forbids.

Two findings were my own audit being wrong, and are worth recording
because both would have produced a confident, false correction:

1. It called `ClientPO.po_number` "required and not sent". The TMS
   issues that number itself, and the CRM's own `THEIRS_TO_NUMBER`
   already said not to send it — I had subtracted that list from the
   omissions and forgotten to subtract it from the requirements.
2. It called `client_name` an invented field. The TMS stores
   `client_id`, an integer into its own client master, so `client_name`
   is a **match key** — it is in the pack so the person importing can
   find the right client. Acting on "invented" would have deleted the
   one column that makes the pack usable. `LOOKUP_KEYS` now names the
   difference.

The audit reads the TMS repository rather than a copy of its field
names kept here, and refuses to run without it. A vendored list is a
list that drifts, and drift is the whole problem this check exists to
find.

## 55 — the Godrej rule is why an auto-merge needs a GSTIN check

Module 2.3: "Auto-merge where they share the same GSTIN, the same
email domain, or the same name after normalising."

`scripts/account_duplicates.py` already finds clusters on exactly
those three kinds of evidence, and it deliberately refuses to merge
in bulk — its docstring says "There is no 'merge everything it
suggested'". The 2 November decision overrides that caution, so the
auto-merge is built; what it needed was a line drawn somewhere, and
the same pack says where:

> **Godrej block:** apply it only to the Godrej company already named
> in the 21 Oct decision. Other Godrej group companies are not
> blocked.

Godrej & Boyce and Godrej Agrovet share `godrej.com`. `clusters()`
joins on any single piece of evidence, transitively, so a domain
match puts them in one cluster — and an auto-merge on the domain rule
would make them one account, after which one block decision applies
to both and the pack's own sentence becomes impossible to honour.

So the rule is: **two different GSTINs in one cluster is never one
customer**, whatever else matches. A GSTIN is a legal identity.
`merge_confidence()` auto-merges only on one GSTIN across the
cluster, or on names that all normalise to the same string with no
GSTIN conflict; a cluster joined only by a shared email domain goes
to Seema's queue, because a corporate domain is shared by a group and
not by a company.

That leaves fewer automatic merges than the decision's wording
suggests. It leaves none that are wrong.

## 56 — the "inactive owners" in Module 2.2 are mostly active

Module 2.2 rule 2 is headed "Inactive owners, as per the agreed map"
and names Manju, Manas, Suketu, Venkatesh, Setu, Sumit, Dhanashree,
Laxmi and Abhishek Singh.

Six of those resolve to employees who are `is_active = 1` in the CRM
today: Manjurul Hoque, Venkatesh Ramarao Althada, Sumit Mondal,
Dhanashree Pawar, Laxmi Ram Singh and Abhishek Singh. Dhanashree is
confirmed active in the same pack, twice.

So the map is applied as what it *does* rather than what it is
called: a re-pointing of CRM account and lead ownership. Nothing in
`accounts_module2.py` writes `is_active` — a test asserts that
against the module's own source, with comments stripped, because the
first version of the test matched a comment instead — and `plan()`
prints each active person under the heading so the list can be
corrected rather than applied under a description that does not match
the data.

Two of the nine names match nobody in the employee master at all
(Manas, Setu, Suketu), which is reported and not an error: somebody
who has already gone from the master has nothing left to move.

And Tamal Das, named in rule 1 for Sarens, is not in the employee
master either. That refuses rather than guesses, as it did in
CRM-COCKPIT-02 §6.6: a wrong employee code in an owner column is
silent, and the account simply stops appearing on anybody's list.

## 57 — I rebuilt a feature that already existed

Module 3 asks for a monthly RFQ target per salesperson per vertical.
I grepped for `kpi_target`, found a bare table from a dashboard
script with no model and no readers, concluded there was nothing
there, and wrote a model, a service, a screen and a migration.

`rfq_targets` already existed. CRM-LEAD-JOURNEY-01 §6.3 built it,
`app/models/rfq_target.py` was its model, and the journey screen, the
RFQ reports and the pilot readiness check all read it. I had written
my file over it with `cat >`.

The test suite caught it — four tests in `test_lead_journey` and
`test_rfq_reports` went red, because their column is `target_rfqs`
and mine was `target`, and one of them is called
`test_a_target_is_unique_per_month_vertical_and_person`. The original
file came back out of git, and Module 3 is now two nullable columns
on that table (`source`, `basis`) and one new table beside it
(`rfq_target_history`), which is all it ever needed.

Worth recording for the grep that misled me. I searched for the
*name I expected the feature to have* rather than for the thing it
does, and a single negative result read as proof of absence. The
cheap check I skipped — `grep -rn "target" app/models/` — would have
taken one command.

## 58 — a target of zero is not a target

Module 3.1 pre-fills from "each person's average monthly RFQ count
over the last 4 months, plus 20%".

Three tables could answer "how many RFQs did this person raise", and
on production they do not agree: the RFQ Chain's own `rfqs` table is
effectively empty, so a seed that read it would propose 0 for
everybody. Zero plus twenty per cent is zero — a target that passes
every review automatically, and that looks on the screen like a
number somebody chose.

So `counted()` tries `rfqs`, then leads that reached RFQ Generated,
then `rfq_date`, and **names the source it used** in each target's
basis. A number nobody can trace is a number nobody will accept
being measured against.

And where a person has no history at all, the seed proposes nothing
and lists them. The screen says why, and an administrator sets theirs
by hand. That is the honest outcome and it is also Module 3.2's
screen doing its job on day one.

One more thing the empty case found. `_vertical()` used
`vertical_heads.master_vertical()` for every name, and that function
falls back to **Management** for anything it cannot map — so on a
database whose vertical master has not been loaded, every vertical
became Management and the entire module silently proposed nothing for
anybody. One of the four selling verticals is now itself without a
lookup, and only the aliases go through the table. A name that is
already a master vertical should not need a table to stay itself.
