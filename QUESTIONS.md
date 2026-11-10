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

## 59 — the CRM had no brand check, only a brand fixer

PROCAM-DECISIONS-02NOV §9.4 asks for the brand check run "across CRM
and TMS … Zero hits before go-live". The TMS has
`scripts/brand_check.sh` and has had it since the BRAND-NEUTRAL-01
sweep. The CRM had `scripts/rebrand_procam_ai.py`, which *fixes* text
and cannot answer "is it clean now" — so a release could be checked in
one repo and merely hoped about in the other, which is how rule 0.6
would eventually be broken without anybody noticing.

Ported, and the first run found one thing that genuinely reached a
person. `/api/outreach/generate` answered, when the key was not set:

> AI Outreach not configured — set ANTHROPIC_API_KEY on the server.

A 503 body rendered in the UI, naming the vendor in the one place a
salesperson would read it, and telling them to go and set a server
environment variable — which is not their job either. It now says the
feature is not switched on yet and to raise a ticket.

The other 52 hits were server-side: the adapters that import a
vendor's SDK, the module whose job is to know the vendor names so it
can strip them, and the config auditor that lists the key names.
BRAND-NEUTRAL-01 §4 allows a provider name in server-side
configuration, so those are excluded **by path**, with the reason
written next to each. Four were comments and docstrings that named the
model for no reason and now do not.

Two things worth recording about the check itself.

The five adapter lines inside `app.py` cannot be excluded by path,
because `app.py` is also the whole application — so they are in
`scripts/ai_terms_allow.txt` as the exact lines of code, with no file
or line number. A line number drifts the moment anything above it is
edited; a file name there would hide every future vendor name added
anywhere in a 6,600-line file.

And the allow-list is fed to `grep -vFf`, which treats **every** line
of the file as a pattern — including a blank one, which matches every
line and silently passes the whole check. The TMS allow file is empty
today, which is precisely when that trap is invisible. Both scripts now
strip comments and blanks before the grep.

## 60 — rule 2 of Module 2.1, followed literally, creates an account called "Unknown"

The dry run on live data proposed creating accounts named **Unknown**
for leads #10096 and #10098. Rule 2 of the decision says "create the
account from the name the user typed", and that is what somebody had
typed.

The fault is not new and is already named in this codebase.
`account_match.not_a_customer` refuses to **link** a lead to an account
called Unknown, and its docstring says why: every lead filed under a
placeholder looks placed, so it never reaches Account check again, and
the histories of several real customers pool under one meaningless row.
Account #847 "New Lead" holds both fried-sped.de and
jindalstainless.com today. Account #983 "Unknown" lists
`procamgroup.in`, so every internal forward matched it on an exact
rule — and had only not linked because the flag was still off.

That guard could not catch this one, because at the moment rule 2 runs
the account **does not exist yet**. So the same rule now applies one
step earlier, at creation: `why_not_an_account_name` refuses a blank, a
placeholder, punctuation only, anything containing "procam", and any
candidate whose sender domain is one of ours.

Three things worth recording about how it is built.

**The list is not written out again.** `PLACEHOLDER_NAMES` and
`OUR_DOMAINS` already existed and are read by the matcher and by
`not_a_customer`. A second copy is exactly how a word gets added to one
and not the other, and then an account nobody may link to is one this
module happily creates. A test asserts the words are not re-typed in
`accounts_module2.py`. The shared set gained `to review`, `review`,
`tbd`, `new`, `lead`, `enquiry` and three more — which strengthens the
linking guard at the same time, for free.

**A refusal is a different destination from a review.** These leads do
not go to Nilesh's Thursday review; they **stay in Account check**,
where a person reads the enquiry and types the customer's name. That is
the one thing no rule can do, and it is not a decision for a director.
The "no name and no domain" case moved there too — it had been going to
Thursday, and nothing about it needs deciding.

**The guard is asked twice.** Once when the plan is built, and again in
`apply_queue` at the moment of writing. Creating is the irreversible
half: an account called Unknown cannot be undone by deleting it once
leads, notes and quotes point at it, and the bulk log restores column
values, not rows.

Also added, because the report prints thirty rows and the
thirty-first is the one that creates Unknown: `--csv` writes **every**
proposed account — lead, typed name, proposed account, owner, why that
owner, vertical, rule — one line per lead rather than per account, so
two leads collapsing into one account is visible. That is either the
same customer twice, which is right, or two customers whose typed names
normalise together, which is not.

## 61 — the four rules that stop Module 2.1 creating junk accounts

The second dry run, after QUESTIONS.md 60, found more. Four rules now
sit in front of rule 2, and the order matters because each is cheaper
than the next.

**Not a customer at all.** Before any of the four decision rules, the
lead is asked whether it is an enquiry. A lead from a conference
organiser has a company name, a sender domain and nothing else wrong
with it — so every rule would clear it happily and create the account.
`not_relevant_class` reads the Master Data block list first (a decision
somebody already made, read rather than copied) and then five classes:
marketing, events, portals, insurance and broker solicitations.

The `portals` class is narrower than the word suggests, and
deliberately: it names the directory sites that solicit **us** —
JustDial, IndiaMART, TradeIndia and the "premium listing package" mail
— and **not** the procurement portals that buy **from** us.
`junk_suggest` already records the measurement that settles this:
`noreply@*` was kept off the block list because 226 of the 247 leads it
would have held were freight load postings and SAP Ariba RFQ
invitations, "two of which are open opportunities with owners right
now". So Ariba, GeM, Coupa, Jaggaer and anything with "tender" or
"eproc" in it are excluded from the whole classifier by name, before
any pattern is tried. A tender portal is how a large client sends an
RFQ.

The same restraint shapes `insurance`. It looks for a *solicitation* —
"we offer cargo cover" — not for the word insurance, which appears in
half of all freight enquiries because the client is telling us who
insures the cargo. A test holds that.

**Too short, or a word for what the business does.** Under four letters
once the punctuation and legal suffix are off, or the whole name is one
of `GENERIC_NAMES`. This is the rule that costs the most false
refusals: BEML has exactly four letters and ABB has three, so ABB
reaches Account check for a person to name. That is the right way round
— a person naming ABB costs a minute, and an account called "Global"
collecting every enquiry whose sender typed their department costs a
year.

**Already an account under another spelling.** `company_match.norm`
keeps the spaces, which is right for its own job: it is the key the
matcher LINKS on, and linking on a squashed key would join two
companies whose names differ only by a word break. So "Hitachienergy"
and "Hitachi Energy" are two keys to it and one company to a reader.
`squash` adds the second key, and `looks_like_existing` uses it to
**propose** a link rather than make one — which is why it can afford
comparisons that would be unsafe to act on automatically.

Aliases are in that index, and they are what resolves "Lntecc". A merge
keeps the loser's spelling as an alias of the survivor, so the alias
list is the only place the fact "LNT is Larsen & Toubro" can live. An
alias prefix is honoured from three letters; an account *name* prefix
only from eight, because proposing that "Global Calcium" is "Global"
would be wrong often enough to teach people to ignore the suggestion.

**One account per company, however it is spelt.** Grouped on `squash`
and given one canonical spelling. The tie-break took three goes:

* raw length picked "T.Q. Cert" over "TQCERT", because the dots made it
  longer without saying anything. So punctuation count comes first;
* then the most words, which gives "Drewes Group" over "Drewes" — the
  extra word is information, and the shorter name can be read out of
  the longer one;
* then the one that is not shouting: "Westfracht" over "WESTFRACHT". An
  account name is printed on a quote.

`apply_queue` already collapsed case variants through its own
lower-cased cache. Doing it in the plan instead means the number in the
preview is the number of accounts that will exist, which is what rule
0.3 asks Nilesh to approve.

**And the sections now stand alone.** `--apply --only merges` does the
merges and nothing else — it already did, but it opened an empty
bulk-log batch and printed an undo command for it, which undid nothing
and read as though the merges were reversible. The batch is now opened
only for the sections that write through it. `notrelevant` is an
opt-in section that is not in the default set at all: marking leads not
relevant is its own decision with its own preview, and a section that
runs only when named cannot be swept along by somebody running the
whole thing.

## 62 — no bulk account creation, and three things that followed

Decided 2 Nov after the second dry run: **no account is created in
bulk.** Every lead in `module2_accounts_to_create.csv` stays in Account
check for a vertical head to confirm on screen.

`CREATE_ACCOUNTS = False` in `accounts_module2`, and the rules still
run and still report. The plan keeps marking those rows `create` and
naming the account it would have proposed, rather than quietly dropping
the proposal, because that CSV **is** the list the heads work from. What
changed is that `apply_queue` will not write one, counts what it held
back, and says so.

Linking is untouched. Rule 1 — a lead whose sender domain or whose
normalised name is exactly an existing account — still links, because
that is not a new record and the certainty is the one the matcher uses
everywhere else.

The creation test was kept rather than deleted, with the switch flipped
for its own duration: when a head confirms an account on screen the same
dated remark has to appear, and that test is the only thing holding the
creation path to rule 0.2.

### The fifteen named senders, and the domain that must not be guessed

The decision names fifteen senders and asks for their domains on the
Not-relevant classes. It names them by **name**, and a domain invented
from a name is a rule that either never fires or fires on somebody
else: "MeetCleanBase" is not obviously `meetcleanbase.com`, and
"Akmglobal-Mailer" is a mailer for a firm whose own domain it may not
share.

So `scripts/2026_11_02_not_relevant_senders.py` finds the leads first,
by typed name, sender address and sender domain, and writes the domains
they **actually arrived from**. A name matching no lead gets no rule:
a domain nobody has seen mail from is not a rule worth having. On the
test data, twelve of the fifteen matched nothing.

**The correction worth recording.** The first version counted every
domain it saw, including on the leads it had just refused to touch — so
Opterix, whose one lead is at Quoted, had `*@opterix.example` added
anyway, and the client who sent that quote would have had their next
mail filed as junk. Rules are now built from eligible leads only, and
the domains excluded for that reason are printed rather than dropped
silently.

### Two sender lists, not one

`system_senders` is the ingest block list: matched mail never becomes a
lead at all, and is written to `mail_ingest_log` as blocked. "Future
mail goes there" means the Not-relevant **queue**, not the ingest log —
so the fifteen go on a new Master Data list,
`not_relevant_sender`, read by `not_relevant_class` right after the
block list. The mail still becomes a lead; the lead classifies as not
relevant.

The difference is not academic. A directory portal sends one real
enquiry a year, because a client's buyer sometimes mails from a portal
account — invisible if blocked at ingest, correctly filed if listed
here.

The list has **no seeds**. A sender named in code is a sender nobody
can take off, and a test asserts none of the fifteen appears in the
module. `why_not_a_pattern` refuses a Procam domain (every internal
forward would be filed as not relevant, and a forward is how most real
enquiries arrive), a free-mail provider, a catch-all, and a bare domain
with no `@` — which would be a rule that silently never fires, because
a sender address is `somebody@domain`.

### Why "Lntecc" did not link: there was no alias

`squash("Larsen & Toubro")` is `larsentoubro` and `squash("Lntecc")` is
`lntecc`. Neither is a prefix of the other and neither contains the
other, so nothing but an alias can connect them — and `aliases`
defaults to an empty list, filled only when a merge happens, since a
merge keeps the loser's spelling as an alias of the survivor. An account
never merged into has none. That is the whole answer, and
`scripts/2026_11_02_lnt_alias.py` confirms it on the live data rather
than assuming it.

It adds the alias and nothing else: no rename, no merge, no lead
linked. And it refuses to choose between two candidate accounts — a
company that size usually has one per division, and which of them owns
the alias decides where every future "Lntecc" lead lands.

One bug caught while writing it: `aliases` is a JSON column, and
SQLAlchemy does not notice a list being mutated in place. An
`aliases.append(...)` saves nothing and reports success. The list is
reassigned, and a test asserts the source does that.

## 63 — the live run marked nothing, and the reason was a bug in the gate

`scripts/2026_11_02_not_relevant_senders.py` on production: **0 leads
would be marked**, and all fifteen senders refused with some
combination of

> it is at New, not New Opportunity; somebody wrote a note on it

### "it is at New, not New Opportunity"

`junk_delete.ELIGIBLE_STAGE` is `'New Opportunity'` and the check was
`stage != ELIGIBLE_STAGE`. Its own comment says what it meant —
*"the stage a lead has to still be at. Anything further means somebody
worked it"* — and the code is the opposite: **`New` is the FIRST stage
on the journey and `New Opportunity` the fourth**, so an equality test
refused every brand-new lead, which is the exact population the junk
module exists for.

Not an edge case and not new. The Delete button has been disabled on
every lead at New since the module shipped, and nobody noticed, because
a disabled button looks like a rule rather than a fault. It is now a
ceiling: eligible at or before New Opportunity, refused past it.

Splitting it was the rest of the fix, because one key meant three
opposite things:

    stage    a stage nobody recognises — a data oddity
    worked   past the ceiling. A lead at Quoted is a lead somebody
             quoted, which is sales work
    closed   terminal. Three of the live Promedia leads are at Lost,
             and reopening whether a closed lead was junk is not this
             module's business

`worked` and `closed` are in `WORK_KEYS` with the quote and the
outgoing mail. Nothing in that set can be ignored by any caller,
whatever it asks — `mark_not_relevant` refuses the request and names
the key.

### "somebody wrote a note on it"

This was folded into `activity` with logged calls and completed tasks.
They are not the same evidence. A logged call is unambiguous. **A note
on a marketing lead is very often the word "junk"** — somebody read it,
decided, and had no button to press. Counting that as proof the lead
matters is exactly backwards.

So `note` and `task` are their own keys; all three still block by
default; and `mark_not_relevant` grew an `ignore` list so a run
authorised to overlook a soft blocker can name it and nothing else.
`--allow note` is how this run does it, and the dry run **prints what
each note actually says** first, because nobody can judge "somebody
wrote a note on it" without reading it.

With `--allow note`, the simulation files Gastechevent, Luxuryescapes
and All India Info-Database, and still refuses the Lost Promedia leads.

### Two smaller things the live run settled

**The domain rule follows the soft/work split.** A domain is proposed
when the only thing in the way is soft; it is held back when there is
sales work on every lead from it. The earlier version held back a
domain whenever the lead was refused for any reason at all, which with
this bug in place meant all fifteen — "(none — no domain was
observed)".

**`*@gmail.com` is what "All India Info-Database" mails from.** No
domain rule can ever work for that sender, and `why_not_a_pattern`
refuses it: several real customers mail from a personal provider. The
report prints it with `!!` so the gap is visible rather than silently
absent. If that sender needs catching, it has to be by name.

### And the L&T alias: four candidate accounts

The live run found four, and the earlier report gave no way to tell
them apart. It now prints the **lead count against each**, because the
account the business is already using is the one its leads point at —
on production that is #74, holding 106 of them. It still refuses to
choose: merging the four on `/admin/account-duplicates` is the real
fix, and it gives the survivor the losers' names as aliases for
nothing, which is where "Lntecc" would have come from in the first
place.

## 64 — "somebody wrote a note on it": nobody had

The notes printed by the dry run settle it. Every single one, across
all fifteen senders:

```
CAUTION: This email originated from outside of the organization. Do no…
[10-Oct-26] Owner auto-assigned → Vijay T V (same vertical)
[10-Oct-26] phone "+91-99582-48203" → +91-9958248203 [10-Oct-26] Owner…
From: Sina Deipenau <s.deipenau@futurebattery.de> Sent: Tuesday, 1 Sep
```

Not one was written by a person. They are, in order: the Exchange
external-sender banner, the CRM's own assignment log stamped by a bulk
run, the CRM normalising a phone number and saying so, and a quoted
mail header. The first and last were captured out of the mail body when
the old combined notes field was split into `lead_notes` — which the
model already records: `migrated_from_legacy`, whose comment reads
*"set when the row was rescued out of the old combined field, so a
reader knows it may be email text rather than somebody's note."*

So `_check_note` asked the wrong question. It asked whether a row
existed in `lead_notes`. `note_is_human` now asks whether a **person**
wrote it, on three tests, all of which must pass:

* `migrated_from_legacy` is false — the column exists for exactly this;
* it does not open with a dated bulk stamp `[10-Oct-26]` or a mail
  header or banner. **Matched at the start only**: somebody writing
  "From the look of it they want a low bed" is somebody writing, and a
  note that *opens* with `From:` is a header;
* somebody is named as the author. The screen sets `author` from the
  session on every note typed on a lead, so a note with nobody's name
  on it was not written by anybody.

All three rather than any one, because a bulk run can stamp a note
**and** name the employee it assigned the lead to, which would pass the
author test on its own.

With that, the fifteen clear on their own merits and `--allow note` is
not needed for them. It stays, for a case where a real note says
nothing.

### The same fault, one row along

`_check_human_activity` claimed in its own docstring that
`LeadActivity` "is only written when somebody logs a call, a meeting or
a visit". It is not: **every stage move writes a row with
`kind='stage_change'`** (`app.py:3424`). So the check reported "a call,
meeting or activity is logged on it" on leads where nobody had ever
rung anybody — and did so on two live Promedia leads, read side by side
with the notes above, before anybody noticed.

Narrowed to the kinds that mean contact — call, meeting, visit, email,
rfq, note — and `stage_change` is its own key with its own sentence.
**The outcome does not change**: a stage change is still work, still in
`WORK_KEYS`, still un-ignorable. What changes is that the reason given
is true, and a refusal that misdescribes its evidence is one nobody can
act on.

### What the live run will now do

Simulated against the exact notes above: the five senders whose leads
are at New are filed, their domains go on the Not-relevant list, and
the Lost ones — Promedia, Marineinsight — stay refused, as does
Promedia's genuinely logged call. `*@gmail.com` is still refused and
still printed with `!!`.

Three bugs in one gate, all found by printing what the data actually
said instead of trusting what the code claimed about it. The stage
equality (QUESTIONS.md 63) had hidden the other two: while every lead
was refused for being at New, nobody had reason to read the notes.

## 65 — Archive a lead: the columns were there, the door was not

CRM-USER-ISSUES-02NOV §1, reported as "there's no delete/archive
option, even for super admin."

Two separate things were missing, and only one of them was a button.

**The door.** `is_archived`, `archived_at`, `archived_by` and
`archive_reason` have existed since §58, and so has the writer —
`bulk_admin.archive()` / `unarchive()`, which also writes the
`DeletionAudit` row and the batch event. The only entrance was the bulk
admin screen, which takes a list of ids and is not where anybody works.
So nothing was added to the data model and no second writer was
introduced: `app/services/lead_archive.py` is the per-lead door onto
the one that exists. A second writer would have set the four columns
without the audit row, and the trail would then have been complete for
a bulk archive and silent for the archive a person actually performed.

**The exclusion.** This is the half no button would have fixed.
Thirty-three modules filtered `is_archived` for themselves.
`app/access/scope.leads` — the one function "every board, count,
digest and report goes through", by its own docstring, and the function
that excludes junk-deleted leads for exactly this reason — did not. So
"it drops out of lists, dashboards and reports" was true in
thirty-three places and false in the main lead list, which is the
screen everybody uses. The filter now sits beside the junk one, with
`include_archived=True` for the caller that wants them.

`bulk_admin` queries `Lead.query` directly rather than through the
scope, so its own archived view is unaffected.

**The reason is required in the service, not in the browser.** A reason
enforced in JavaScript is a reason absent from every other caller.
`MIN_REASON = 4` admits "spam" and "dupe", which people mean, and
refuses the stray keypress a mandatory field otherwise collects. Over
200 characters is refused rather than truncated, because filing half a
sentence as the explanation is worse than saying no.

**Who may** is the owner, the vertical head of the lead's vertical, or
a CRM admin — `junk_delete.may_delete`, imported rather than restated.
Two copies of a permission rule drift, and the copy that drifts is the
one that lets somebody through. A super admin is an admin by role, so
"even for super admin" needs no clause of its own.

Archiving an already-archived lead is refused: the second reason would
overwrite the first and why it actually went would be lost.

One accepted change: `tests/fixtures/all_leads_flag_off.sha256`. That
snapshot guards the legacy All Leads list against the lead_v2 pilot's
changes. The archived filter is deliberate and for everybody, not part
of the pilot, so the snapshot was retaken — which is the path the test
itself documents.

## 66 — "A reply creates a duplicate": the column nothing ever wrote

CRM-USER-ISSUES-02NOV §2.

Almost all of this was built and correct. `lead_intake.classify` matches
on the thread headers at step 3 — "deterministic, final, outranks
everything" — and files a reply against the lead it belongs to.
`lead_merge.thread_key()` computes a key. `leads.thread_key` is a
column, indexed, migrated by `scripts/2026_10_30_lead_flow.py`, and its
comment in app.py reads: "Filled at intake; a reply whose thread is
already here joins the lead instead of starting another."

It was never filled. Not by any code path, ever — only by three tests
that set it by hand. It has been NULL in live data since the day the
column was created. Two consequences:

* `lead_merge.candidates()` asks `other.thread_key == mine_thread` for
  its one **certain** duplicate rule. NULL == NULL, so the rule was
  dead code and thread duplicates fell through to the subject
  heuristic, which by its own comment "only ever draws a banner".
* Step 3 still worked, but only via `conversation_id` and stored
  message ids. A reply carrying a conversation we never saw — relayed
  by a colleague, sent from a client that rewrote the headers, or on a
  thread that started before this mailbox existed — had nothing to
  match.

Now filled in both intake paths, and keyed on the **resolved** sender.
Both paths set `_resolved_sender` on a *copy* of the message for the
classifier, so reading the original message would key a relayed
enquiry on the colleague who forwarded it and make two unrelated
forwards from one person look like one thread. I wrote that bug into
the first version of this change and caught it before pushing.

`find_by_thread` now tries the stored key last, after the headers,
because it is the only one of the three that can be a guess. A `c:`
key proves as much as a conversation id and is not windowed; an `s:`
key is subject-plus-domain and is windowed to `lead_merge.WINDOW_DAYS`
(30), because "RFQ" from a regular customer is the same key in March
and in November and those are two different enquiries.

### One thing in §2 I did not do, deliberately

§2 says "then the normalised subject + sender domain **within 30
days**". `find_by_subject` — the existing subject fallback at step 5 —
runs at **180** days. I left it there.

Narrowing 180 to 30 would match *fewer* replies to existing leads, so
it would create **more** duplicates, which is the opposite of what §2
is about. The 30-day window is applied to the new `thread_key` path,
where it is the right bound for a key that can be a coincidence. If
the intent was to narrow the existing fallback as well, say so and I
will — but it should be a deliberate choice rather than a side effect,
because it loosens dedup rather than tightening it.

### The report

`scripts/2026_11_03_thread_duplicates.py`. Report by default, writes
nothing, and there is no `--merge` and will not be one: §2 says "I
decide". `--backfill` fills the NULL keys and is still a dry run
without `--apply`. The report does not need the backfill to have run —
it computes the key in Python from the columns the intake did store —
so the listing can be read and argued with before anything is written.
A test asserts the script's only assignment is `thread_key`.

Pairs are counted as certain (conversation id) or likely (subject and
domain) separately, because they are not equally certain and whoever
decides what to merge needs to know which is which. The window is
measured between the two leads, not against today, so a pair from
March is still a pair in November.

## 67 — "Some PICs don't get the new-lead mail": five silences

CRM-USER-ISSUES-02NOV §3. The question was where the recipient
resolution fails. The answer is that it failed in five places and
reported none of them.

`app/services/notify.py` is the one function every notification goes
through, and it returns `{'notified', 'emailed', 'suppressed',
'error'}`. `lead_assignment.py` — the caller that sends the new-lead
mail — called it as a bare statement and threw the dict away. So every
one of these returned an `error` that nobody ever read:

1. **No address on the employee.** `'no email address on file'`.
2. **`outbox.enqueue` refused it** — already queued, muted, or not an
   internal address. `mail_policy` allows only `@procamlogistics.com`
   and `@procamgroup.in`, so a PIC whose record carries a personal
   address was refused at the transport and nothing said so.
3. **FEATURE_EMAIL_NOTIFY off.** The direct transport is used, with no
   outbox row, no retry and no record. One throttled response from
   Office 365 — which caps a mailbox at thirty a minute — lost the
   notification outright.
4. **An exception.** `log.exception` and `return False`.
5. **Suppressed as a recent duplicate**, which is correct behaviour and
   was also invisible, so "nothing was sent" and "nothing needed to be
   sent" looked the same.

**Inactive employees were not checked at all.** §3 lists it as a
candidate and it turned out to be a real gap: a leaver's address was
mailed exactly as if they were still here, the send succeeded, and the
lead looked notified. Now refused, with the address kept on the row so
the reassignment is obvious from one line.

### Why a new table and not a column on email_outbox

Because the rows this is about never became outbox rows.
`/admin/email/log` is a log of messages that reached the queue, and a
message missing from it is indistinguishable from a message nobody was
owed. That indistinguishability is the bug. `notification_attempts`
records the attempt, and `outbox_id` links to the queue row where one
was created: this table answers "did the CRM get as far as trying?",
the outbox answers "what did the mail server say?".

Written in `notify.send`, not at the call site. Recording in
`lead_assignment` would have logged the assignment mail and missed the
other callers — the same mistake as filtering archived leads in
thirty-three modules and not in the scope (QUESTIONS 65).

### Retry: three, and what three means

§3 says "retry failures 3 times". Two kinds of failure need two
answers, and treating them alike would be the easy wrong thing.

A **transport** failure can succeed on a second try. Anything queued
already gets five attempts with back-off from the outbox, and nothing
was added there. The gap was the direct path, which had none; it now
gets three.

A **resolution** failure cannot. "No address on file" says the same
thing three times and buries the one fact that matters, which is that
somebody must fix the record. Those are recorded and left for a person.
`notify_log.retryable()` tells them apart, and treats an *unknown*
error as retryable — the cost of being wrong is three attempts rather
than a notification lost for good.

A person retrying from the screen is deliberate and is allowed either
way, because somebody who has just corrected an address is exactly the
case for it.

### The screen

`/admin/notifications`, beside the email log, defaulting to the
failures. The address is shown even when it was refused: "failed,
somebody@gmail.com, not an internal address" answers the question in
one line, and storing it only when it worked would have hidden the most
common cause. Employee codes, never names.

A test asserts the log can never break what it is logging, and a source
guard asserts `notify.send`'s result is not discarded again.

## 68 — "Forwarded mail never reaches the CRM": the log that was bypassed

CRM-USER-ISSUES-02NOV §4. The log, the screen and the re-process button
all already existed: `mail_ingest_log`, `/admin/mail-ingest`, and
`create-lead`. What did not hold was the promise `_record_ingest` makes
in its own docstring — "one row per message, whatever happened".

**Six paths returned without writing a row.** Four in
`process_single_message`, found by reading, and two more found by the
AST guard in the new test after I thought I had them all:

* `already ingested`;
* `previously purged as irrelevant` — a permanent verdict, silently;
* `already classified`;
* **`blocklist.check`** — the worst one, below;
* the **client block register**, which wrote an `EmailEvent` the Intake
  boards show but no `mail_ingest_log` row, so the enquiry was absent
  from the screen somebody actually opens to ask where their mail went;
* `return _attach_outbound(...)` — a false positive. It logs inside the
  callee, which the guard now checks rather than assumes.

And `pipeline.py`, the poll path, never wrote a single row in its life.

### The denylist is where the silence was

`blocklist.check` drops by domain from `data/blocked_senders.txt` and
by local part on **any** domain: `_BULK_LOCAL_RE` matches `mailer@`,
`bulk@`, `broadcast@`, `digest@`, `campaign@`, `offers@` and `deals@`.
A real enquiry relayed from an address shaped like that was dropped
with no trace anywhere — not a lead, not a timeline entry, not a log
row. From outside the CRM that is indistinguishable from mail that
never arrived, which is exactly how §4 was reported.

Now recorded with outcome `blocked` rather than `skipped`, so the
denylist's work is readable without scrolling past every "already
ingested", and the row carries the pattern that matched so the rule can
be corrected rather than guessed at.

**`force` now overrides the denylist**, as it already did the
system-sender block and for the same stated reason: a person replaying
a message by hand has already decided. Without that the rows this
change puts on the screen could be read and never acted on — §4's
"re-process one" would come straight back to the denylist and skip
again.

### The four things §4 asked me to check

1. **Forwarded mail with an attached `.eml`** — a real gap, and *not*
   fixed here. `graph_client.list_attachments` filters to
   `fileAttachment` and drops `itemAttachment`, which is what Outlook's
   "forward as attachment" produces; its own docstring says those
   "need different handling" and that handling was never written. A
   dragged-in `.eml` file arrives as a `fileAttachment` and is fine; a
   forward-as-attachment loses the attached message. The lead is still
   created — this path captures everything — but with the outer
   forward's empty body, so the enquiry itself is lost. Unwrapping
   `itemAttachment` needs `$expand=microsoft.graph.itemAttachment/item`
   and I cannot test Graph behaviour from here, so it is written up
   rather than guessed at. **This is the one piece of §4 I have not
   built.**
2. **HTML-only bodies** — not a fault. `parser._get_body_text` converts
   `contentType == 'html'` through `_html_to_text`. Tested.
3. **The filters and sender rules, "including the 14 rules added this
   week"** — those fourteen are *not* the ingest denylist. They went to
   the Not-relevant classes (QUESTIONS 62), which classify a lead that
   has already been created and cannot drop mail; the module refuses
   any pattern matching a Procam domain precisely because a forward is
   how most real enquiries arrive. They are not a candidate, and a test
   now says so.
4. **The poll window** — not in play. `service.poll_should_run()` is
   False unless `EMAIL_INGESTION_MODE=azure_api`, and the default is
   `mailbox`: the 09:00 poll was retired and the webhook is the only
   live path. So there is no window to widen. The corollary is worth
   stating plainly: **the webhook is a single point of failure.** If the
   Graph subscription lapses, no mail arrives and nothing is logged,
   because nothing looked. There is a renewal timer
   (`procam-crm-graph-subscription.timer`, renewing daily against a
   ~2.9-day expiry) and `app/ops/checks.py` watches it, so the guard
   exists — but it is worth knowing that this is what it guards.

## 69 — Three the full suite caught

All three were mine, and I checked rather than assumed: the suite was
**5032 passed, 0 failed** at d8f01a1 in a clean worktree, so none of
them was pre-existing. I had initially leaned towards "pre-existing
pollution" on the third and was wrong.

**1. Three ungated routes.** The archive routes checked
`session.get('emp_code')` inline, which `scripts/generate_reference_docs
.py` cannot see, so the route audit reported them as open. Listing them
in `PUBLIC_ROUTES` would have been a lie — they are not public. They
now carry an `@_archive` decorator registered in `_GATES` with its
reason, which is what every other module does. An audit with
known-good exceptions in it is an audit people learn to ignore.

**2. The Admin bin would have been empty.** `mark_not_relevant` sets
`is_archived` as well as `junk_deleted` — deliberately, so the modules
that already hid archived leads hid junk too without being changed.
That makes the two filters overlap, and my new archived exclusion in
`scope.leads` therefore removed every junked lead from a caller that
had explicitly asked for them with `include_deleted=True`. The live bin
survived only because `junk_delete.deleted()` queries `Lead.query`
directly; the contract the test encodes was broken, and the next caller
to use it would have got an empty bin. Now a caller that asks for the
deleted rows keeps them, and ordinary archived rows still need
`include_archived`.

**3. Id reuse across modules, which this repo already documents.**
Every test module shares one database — the first
`os.environ.setdefault('DATABASE_URL', ...)` to run wins for the whole
process — and SQLite reuses row ids after a delete. My four new modules
shifted the id sequence, so `test_lead_triage`'s "Six Hours Co"
inherited another module's `LeadAssignmentHistory` and `Notification`
rows, and a one-row assertion saw three.
`tests/test_junk_delete.py::_purge` describes this exact trap in a
comment. Fixed by purging those tables in the triage fixture, which
had no teardown at all, rather than by loosening the assertions: the
counts are the point of those tests, and tolerating strays would stop
them noticing a second assignment, which is what that test exists to
catch.

## 70 — Forward-as-attachment: reading the enquiry, not the covering note

CRM-USER-ISSUES-02NOV §4, second pass. QUESTIONS 68 left this as the
one piece of §4 unbuilt, because it needed a Graph call I could not
test from the laptop. Decision taken 3 Nov: build it.

### Two kinds of forward, and only one of them worked

Outlook's **Forward** puts the original into the body as text under
`---------- Forwarded message ----------`, and the parser has unwrapped
that for months: `analyze_forward` promotes the customer over the
colleague who relayed it.

Outlook's **Forward as attachment** — and dragging a message onto a new
one, which is the default on Outlook for Mac — does something else. The
enquiry becomes an `itemAttachment`, a whole Outlook object hanging off
the outer mail, and the outer body is the covering note or nothing.
`graph_client.list_attachments` filtered those out and its own
docstring said they "need different handling".

**This never looked like missing mail, which is why it lasted.** A lead
was still created — this path captures everything — but out of "FYI,
please quote": no customer, no route, no cargo, no weights, and the
enquiry in an attachment nobody read. It presented as an empty lead,
not as an absent one. `test_without_the_unwrap_the_colleague_is_the_lead`
asserts the old behaviour so the fix cannot be quietly reverted.

### Why it renders text rather than parsing the attached object

The tempting move is to hand the attached Graph message to the parser
instead of the outer one. This does not, because the covering note
matters: who forwarded it, what they said, and that it was relayed at
all are things the CRM records, and swapping the object would lose
`forwarded_by` and the note with it.

Instead the attached message is rendered into the exact text shape an
ordinary forward has and spliced onto the end of the outer body. Then
`split_forwarded_body`, `analyze_forward`, the sender promotion, the
signals, the AI extraction and `forwarded_by` all work unchanged —
because from their point of view this is now an ordinary forward. One
code path for both kinds, and it is the path already proven on 501 real
messages rather than a second one beside it.

### Two traps, both found while building it, both now tested

1. **The forward cache.** `analyze_forward` caches its verdict on
   `msg['_forward']` and returns it unconditionally next time. Anything
   that asked before the splice had cached "not a forward" against the
   covering note, and that stale answer would have outlived the body —
   the splice would land and change nothing. The cache is popped.
2. **An HTML outer body.** Appending plain text to an HTML body leaves
   `_get_body_text` running an HTML strip over the rendered headers,
   which eats anything in angle brackets — including every address. The
   outer body is converted to text *first*.

### The cost, and the gate

One extra Graph call per message, because
`$expand=microsoft.graph.itemAttachment/item` is the only way to see
inside one and it is per-message. Gated three ways before any call:
`hasAttachments` must be true (free, already on the message); the body
must not already carry a resolvable forward (then the enquiry is in
hand and fetching is waste); and a Graph failure returns empty rather
than raising, so the outer message still becomes a lead exactly as it
did before. Messages with only file attachments cost one call and find
nothing — that is the price of the ones that matter, and it is paid on
the create path, not on every refusal.

Several attached messages: counted, the first read, and the count put
on the ingest row. Guessing which of a relayed batch the lead is about
would be worse than saying so.

Both intake paths unwrap, with a test asserting it. A backfill that
built leads out of covering notes would be a second, quieter version of
the same bug.

### The --check script

`scripts/2026_11_03_attached_forwards.py --check`. Read-only by
construction: there is no `--apply`, a test asserts the module never
calls `add`, `delete` or `commit`, and `--check` is required rather than
default so running it by accident does nothing. For each recent message
it prints whether an attached message is there, who it is *really*
from, what the parser would extract, and what the CRM holds for that
message today — so the before-and-after is visible on real mail rather
than asserted. Where a lead already exists naming a different sender,
it says so: that is the population the fix is for.

### The 180-day fallback stays

Confirmed 3 Nov: the 30-day window applies to the new `thread_key` path
only, and `find_by_subject` stays at 180. Narrowing it would match
fewer replies and so create more duplicates. QUESTIONS 66 has the
reasoning; this records the decision as taken rather than open.

## 71 — "(keep?)" was the oldest, and the oldest is the husk

Reported 3 Nov, after the thread-duplicate report was read against the
real data: no merges are needed, because in every pair the extra lead
is already Lost — and the lead the report suggested keeping was that
Lost one, because it was the older of the two.

That is backwards for the way these pairs come about. A reply arrives
on a thread the CRM did not recognise, so a second lead is created; a
person notices the first copy is empty and marks it Lost; the owner
works the second. The oldest is therefore the copy with nothing in it,
and the newest is the one carrying the enquiry.

### The order

Most advanced live status first, then newest:

    Won > Under Negotiation > Quoted > RFQ Generated >
    New Opportunity > Business Discussion > Call Done > New >
    (unrecognised) > On Hold > Not Interested > Lost

The five the request named sit in that order. The rest are not written
out again in the script: `advancement()` reads the position off
`stage_map.JOURNEY`, so the report cannot drift from the order the
funnel, the board and the reports all use, and `stage_map.canonical`
folds a retired name like "Visit Done" onto the stage it means — a
lead nobody has saved since the rename is not the husk.

Two calls beyond what was asked for, both stated here rather than
buried:

**Won ranks highest**, above Under Negotiation. Won is the far end of
`JOURNEY`, not a dead end beside Lost, and a thread that produced a win
should keep the win.

**An unrecognised stage name ranks above every closed state and below
every live one.** It is probably something live, so it should beat
Lost; but letting a word nothing recognises outrank a real Under
Negotiation would be worse. `CLOSED_ORDER` is asserted against
`stage_map.TERMINAL` in a test, so a new closed state has to be placed
deliberately instead of landing somewhere by accident.

### Still only a suggestion

§2 said "I decide", and that has not changed. The report prints
"(keep?)" against one line of a group it prints in full, writes
`suggested_keep` in the CSV, and does nothing with either. The group
is now printed best-first so the suggestion is the line the eye lands
on. A test walks the script's AST and asserts nothing assigns to it.

## 72 — CRM-FINAL-CLOSEOUT-01: nine items, and what each one really was

Three of the nine were already built and needed a command, not code.
Two were half-built. Four were absent. The split is recorded because
"switch it on" and "build it" look identical in a release note.

### §1 — the flags, and the one that should not go on

`scripts/feature_flag.py --all-on` already did this, and the only
change needed was a way to hold one flag back.

**FEATURE_JUNK_PURGE lets an administrator permanently remove deleted
leads older than ninety days.** It is the one switch in the list that
enables a hard delete, against a standing rule that nothing is ever
hard-deleted. "Every built feature on" almost certainly does not mean
"and the purge", so `--except` exists: the alternative is switching
everything on and then switching one back off, which leaves a window
in which it was on.

It is named and not withheld. A script that quietly held a release
back would be worse than one that turns on more than was wanted, so
`--all-on` still switches it on unless somebody says otherwise — and
prints, every run, the flags it thinks are worth a second look.

An unknown `--except` name stops the run. A typo silently dropped
turns "everything except the purge" into "everything", and the output
would not say so.

### §2 — the sending address, where the failure is silence

The [CRM] tag was already applied, once, in `mailer.send_detail`, so
every path that sends through the outbox or the direct sender already
carried it. What was wrong was underneath: with nothing configured,
`mailer.sender()` fell through to `notifier.sender()`, which is the
**ingest mailbox**. Nothing bounces and nothing errors — CRM
notifications simply arrive from the address the poller reads, and a
customer replying to one comes back in as a new enquiry.

So `sender_problem()` names the three ways it can be wrong — the
ingest mailbox, a crm@ address for a mailbox that was never created,
and no subject tag — and `describe()` carries it onto the health
line. The address itself is now an admin setting read before the
environment, because the person who needs it corrected is rarely the
person who can edit .env and restart gunicorn.

**Nothing was sitting in a queue to release.** `outbox.enqueue`
refused the clarifications *before* writing a row, so there is no
held mail to flip to `queued`. What exists is the audit trail:
`rfq.clarification_held`, one row per attempt, carrying the RFQ, the
outcome and the address. The release script reads those and recomposes
each question from the RFQ as it stands today — not replayed, because
if somebody filled in the site address by hand since, the question has
changed and sending the old one makes Procam look like it is not
reading its own file.

Four refusals, each because sending anyway reaches a customer: the
step has stopped waiting (somebody got the answer by phone — asking
again is worse than never asking), a clarification has since been
sent, there is no customer address, or external mail is still off. The
last one reports STILL HELD rather than claiming a send, which is the
whole point of a check run. And it refuses to release anything at all
while the sending address is wrong: these go to customers, and
releasing a hundred from the wrong mailbox is not a thing to discover
afterwards.

### §3 — already done, and the untested writer behind it

The invented handover fields were replaced on 2 November (QUESTIONS
54). `scripts/handover_field_audit.py --tms <path>` reads the TMS
source and reports **no invented fields** — the command is in the
deploy block so the claim can be checked rather than believed.

The FF account linking and the one-word aliases were also already
built. But `ff_link_accounts.py` had fourteen tests, all of them about
`_names_agree`, and **not one of them ran `--apply`**. A dry run
cannot tell you whether the writer works: it reports the same plan
either way. Four tests now cover the write, the audit row, the
refusal of `--check --apply` together, and a name that disagrees not
being written — before it is run against the production book.

### §4 — "nothing should still say DRAFT", and Sea Freight

Pointing quotes at the master was already done: `quote_build` calls
`attach_to`, and `for_print` is read by the quote screen, the PDF, the
emailed quotation and the job handover. The new script checks that
from the database instead of asserting it in prose.

The markers are the decision. QUESTIONS 53 deliberately left Sea
Freight's DRAFT in place, because nobody had given me clauses and an
unmarked placeholder is worse than a marked one. "Nothing should still
say DRAFT" closes that — and the way to close it is **not** to write
Sea Freight's contract language.

So the placeholder annexure is retired, and quotes in that vertical
print the Procam Standard T&C alone: validity, payment, taxes, scope,
liability — real, approved wording. A quote carrying it and nothing
else is a correct quote. One carrying it with a placeholder stapled
underneath is not. The vertical is named in the output so the
commercial team knows exactly which annexure they still owe.

Retired is not deleted and not replaced: the row is superseded with
`superseded_by_id` left null, so a quote printed under it can still
say what it printed under. The master cannot be retired at all — a
quote with no master prints no standard terms.

**This found a bug in my own change.** `publish()` computed the next
version as `current.version + 1`, and after a retirement there is no
current row, so the next publish would start again at version 1 and
collide with the retired row under the unique constraint on (vertical,
version). The day the commercial team finally writes Sea Freight's
clauses is the wrong day to discover that. Versions now count from the
highest ever recorded for the vertical.

### §5 — the dates, and which direction a bound points

Six date columns on `leads` are filled by hand and most are empty, so
every "days from first contact to quote" figure was computed from the
minority of rows somebody typed into. That is a worse number than
none, because it looks like a measurement.

Four rungs, and every answer carries which one answered: the column,
then the lead's own trail, then a bound, then "not known". An estimate
is **always a timestamp from this lead's own history** read as "on or
before" — never an average, never a typical gap. That line is the same
one `stage_map` already draws, and the reason is that a date invented
from a company-wide average is indistinguishable on a screen from one
somebody typed, while a bound read off a later step is checkable by
the person looking at it.

The direction of the bound is the part that can be wrong, and it is
easy to get wrong. The earliest dated thing on a lead is usually the
first call. Reading that as the bound for the RFQ dates the RFQ
*before* the call — a lower bound printed as an upper one. Only steps
that sit after the milestone on the journey count, and the earliest of
those is the answer.

`firm_only()` is for anything that computes a deadline, an SLA, an
ageing colour or an escalation. Being told you are late on an inferred
date is the one outcome that makes people stop trusting the screen.

### §6 — a lead has a team, not an owner

`assigned_to` holds one code and every screen reads it as "the person
responsible". The second owner covers leave, the vertical head answers
for the number, the account's owner has the relationship, whoever was
handed a task is doing the work this week, the RFQ coordinator
registers the request. All five are in the database. None of them was
on the lead, so "who do I talk to about this" was answered by asking
in a group chat.

No employee name or code appears in the module, and a test asserts it
on the source — a code added later for one urgent case is exactly how
that rule stops holding. Every member resolves through a record
somebody maintains, so a leaver stops appearing because a record
changed, not because this file was edited and deployed.

It is a directory and it grants nothing; a test asserts no access
module imports it. Where a member cannot actually open the lead it
says so rather than dropping the row — a vertical head who cannot open
their own vertical's lead is the sort of thing discovered when they
are asked to approve something at six o'clock.

### §7 — the quotation made in Excel

Until the quote engine covers every vertical, quotations are made in
Excel and mailed from Outlook, so the lead holds no copy of what the
customer was actually sent. When the number is argued about three
weeks later the only copy is in somebody's Sent items.

Three things it will not do, each for a reason:

* **it does not move the stage.** That starts the quote-ageing clock
  and shows in the funnel. Inferring a stage change from a file
  somebody attached would move leads on the strength of a draft. The
  screen offers it; a person decides.
* **it does not touch the value columns.** `quote_value_num` has
  `value_gate` behind it with a margin floor and an approval limit;
  writing a figure in from an upload form walks past both.
* **it fills `quote_no` and `quote_date` only into a blank.**
  Overwriting one is how the CRM comes to disagree with the quotation
  register about what went out when.

On any failure the file is removed again. A row pointing at no file is
a broken download, and that is the one people report.

### §8 — /CRM/ answered 404

`SCRIPT_NAME` was forced from URL_PREFIX so `url_for()` emits the
prefix, but Werkzeug routes on `PATH_INFO`. Where nginx passes its
location through unstripped, PATH_INFO arrives as `/CRM/` and there is
no rule for it — so the one URL printed on the hub tile, in the
signature blocks and in every release note was the one URL that did
not work. Deeper paths survived because people reach them from links
the app rendered itself.

Stripped in the application, not in nginx: the app is the half that
knows its own routes, and a conf change is a separate deploy on a box
that serves the TMS from the same file. Only an exact match or the
prefix followed by "/" is taken off — `/crm2` is a real route that
starts with the same four letters.

### §9 — four task ids that no rule picks

The ids came off a report, not out of `classify()`, so "included" has
to mean included whatever the rules think. `--task` does that, and
keeps a rule's reason where one already applied, because that is the
truer answer and the one a person reading the audit row can check.

A named id that is not an open task **stops the run**. Closing nine
hundred rows while quietly dropping one of four asked for by name
reports a job half done as a job finished.

## 73 — fault 3, half of it: two tasks that waited on one stage name

`lead.qualify` (17 open) and `lead.rfq_review` (22 open) pile up, and
it is the same fault twice: a completion rule naming exactly one
stage, and a lead under no obligation to pass through it.

**`lead.qualify` waited on `Lead / Profile Sent`, and that stage no
longer exists.** `stage_map.ALIASES` retired it onto Call Done, so
nothing writes it and the task could never complete. The October
release note records it as "closes on Profile Sent ✓" — which was
true the day it was written and stopped being true when the stage was
renamed. Nothing connected the rename to the task definition, and
nothing reported the task as unclosable afterwards.

**`lead.rfq_review` waited on `Lead / Quoted`.** That stage does
exist, but RFQ Generated → Under Negotiation is what happens when the
customer negotiates off a verbal, and RFQ Generated → Lost is what
happens when they go elsewhere. Neither route passes through Quoted,
so the task stayed open on a lead nobody was working.

### Past the point, not at it

Each task now closes on every state at or beyond the point its work
belongs to, plus the terminal states. A lead that reached Won
obviously got qualified; a lead marked Lost obviously will not be.
Both are answers, and an open task is not.

The lists are computed from `stage_map.JOURNEY` and
`stage_map.TERMINAL`, never typed — a typed list is how this broke
the first time. A test asserts the computed set equals the journey
past the anchor plus the terminals, so adding a stage fails there
instead of quietly producing another immortal task. Another asserts
`closing_states` contains no stage literal at all.

### The one engine change, and the half of it I did not make

`_state_matches` now accepts `states`, a list, so a rule can name
several closing stages. It also gained `fold_aliases`, which reads
both sides through `stage_map.canonical` so a lead still written
'Visit Done' matches a rule naming Business Discussion.

**`fold_aliases` is passed on the closing side only.** Folding when
closing can only ever close more tasks, which is the point. Folding
when creating would make more tasks — a release that quietly doubled
the task count would be a worse fault than the one being fixed. A
test pins both directions.

### Why the rule and the sweep are one run

The rule alone leaves the existing backlog open forever:
`on_state_change` fires on the next transition, and these leads have
already had theirs. The sweep alone clears the backlog and the engine
immediately starts making more. So `--apply` does both, in one
transaction.

### Completed or Cancelled, and why both

Where the lead advanced through the journey the work happened — a
lead cannot reach Quoted without its RFQ having been reviewed — so
the row is **Completed**. Where the lead died the work became moot
and nobody did it, so it is **Cancelled**. Marking them all Completed
records work nobody did; marking them all Cancelled erases work that
was done. Each row's notes says which and why, and each gets an audit
row.

### What is still open

`deal.lost.debrief` and `competitor.log` — the other two immortals,
and by far the larger number — are **not** fixed. One has no
completion state at all; the other waits on an Opportunity state that
was never built. Neither needs a longer list of stages; each needs a
decision about what "done" means. The script counts them every run so
the remaining backlog stays honest rather than looking solved.

### A note on §9

`scripts/close_named_tasks.py` already exists for ids 1342, 1541,
1283 and 1536 specifically. `--task` on the backlog report is the
general form of the same thing, which is what §9 asked for; either
closes those four.

## 74 — CRM-WA-01: what the PFM team asked for, and what was already there

Five items. Two were already built and needed pointing at, one was
built but not reachable by the people who needed it, and two were
absent. Recorded that way because "it does not work" and "I cannot
find it" need different answers.

### §1 — archive existed; the four reasons did not

Archive shipped on 2 November (CRM-USER-ISSUES-02NOV §1): the button,
the audit row, the exclusion from every list and count, and the
Archived filter. What changed here is the reason.

It was free text, and QUESTIONS 53's reasoning still stands —
"client went quiet" and "budget pulled" are real and neither is a
code. The PFM team asked for four named reasons. **Both are right
about something.** A category is what makes the archive countable:
"how many were duplicates" has no answer when every row is a
sentence. The sentence is what makes one row explicable: "duplicate"
does not say duplicate of what.

So the category is required, the note is optional, and the two live in
the one `archive_reason` column as `category: note`. No migration —
the rows archived before this read back as uncategorised, which is
what they are. `split_reason` deliberately does **not** guess a
category from the words: "duplicate of 10291" looks like the
duplicate category and might have been written by somebody who meant
something else by it.

### §2 — the merge was built, auditable, and irreversible

`lead_merge.merge()` already repointed every foreign key, archived the
loser, and wrote audit rows on both sides. Two things were wrong.

**It recorded counts.** `lead_activities.lead_id: 14`. Fourteen of
what? After the merge the survivor holds thirty activities and nothing
says which fourteen arrived with the loser. The merge was fully
auditable and could never be undone. It now records the row ids, in a
`lead_merges` ledger, with the loser's own pre-merge `stage`,
`lost_reason`, `is_archived` and note beside them — because reversing
also means putting those back, and the merge overwrote all four.

**The audit row recorded the wrong "before".** `old={'stage':
lead.stage}` was built *after* `lead.stage = 'Lost'`, so the trail
said the lead had been Lost before it was merged. Every merge on file
has that. Read before any write now, and a test asserts it.

`unmerge` puts back only the rows in the ledger, and only those still
on the survivor: a row somebody has since moved elsewhere is left
alone, because their decision is newer than the merge. A merge
recorded without ids is marked `partial` and refused — a reversal that
put some rows back and left the rest would be worse than none.

A Won lead still cannot be merged away. "It can be undone" is not a
reason to loosen what may be done.

**The warning at creation** is new, and the design is that the four
signals are not equal. A shared GSTIN is one legal entity exactly; a
shared email domain can be two — `renew.com` carries RENEW POWER and
RENEW PHOTOVOLTAICS, and this CRM has an account called "Mail"
created from `mail.oyaso.com`. So a domain match alone does not reach
the threshold, free mail is never a domain match at all, and the
screen shows what matched rather than a score. A warning that cries
wolf is a warning people click through.

`leads` has no GSTIN column — the GST master is on `companies` — so a
lead's GSTIN is its account's, and a lead with no account has none.
That is an absence, not a mismatch.

The report is pairs, not clusters: "are #10291 and #10433 the same
job" has an answer and "are these six leads one thing" usually does
not. It suggests the most advanced live copy, then the newest, for
the reason recorded in QUESTIONS 71.

### §3 — "PIC" means two different people, and one line caused it

    `leads.pic`          the **customer's** contact person. The lead
                         screen labels it "Contact person".
    `leads.assigned_to`  **our** employee. The lead screen labels it
                         "Primary PIC", and the leads list filters on
                         it under the name `pic`.

One word, two people, opposite sides of the deal.

The missing address was the owner's, and the cause was one line:
`/api/employees` returned `emp_code, name, vertical, role` to anybody
without `admin.employees`, and the lead screen reads that list.
**Administrators saw the address and the sales team did not** — which
is the shape of a bug nobody can reproduce. Withholding it was
inconsistent rather than protective: `/people/<code>` already shows
the same address to every signed-in user. The mobile number is still
not in the bulk list; nothing in the CRM shows that in bulk.

**There is nothing to backfill on the owner's side, and a backfill
would make it worse.** The address lives on the employee record,
which is where it should: copied onto a lead it goes stale the moment
the person changes it or the lead is reassigned, and then the CRM
holds two answers and mails the wrong one. It is joined, not stored.

The instruction — "backfill PIC email from the assigned user's
record" — is safe under one reading of PIC and corrupting under the
other. Writing an employee's address into `leads.email` means the next
person to press "Draft email" writes to us, the mail trail files our
own message as the customer's reply, and the quotation goes to the
wrong mailbox. So the script does the safe half: it fills a blank
customer address from **that lead's own customer mail**, never from an
employee record, never over a value somebody typed, and it reports the
leads that already hold a Procam address in the customer field without
changing them — somebody put it there and may have meant it.

### §4 — already built, and the status was the missing part

The ingest log, the Mail ingest screen, the re-process button and the
"nothing is dropped silently" guarantee all shipped on 2–3 November
(`78e5f48`, `2fe3ba9`): six paths that bypassed the log were closed,
and `tests/test_unprocessed_mail_log.py` walks both intake files and
fails if any branch returns without writing a row.

What nobody could answer was §4's actual question — *when did this
last work, and what went wrong last time*. That is now one read-only
command. It also reports the Graph subscription, because in mailbox
mode a lapsed subscription is the silent failure: Graph simply stops
calling and nothing here errors.

### §5 — the reset existed; knowing which half was wrong did not

CRM login is **not** TMS login. The CRM reads its own `employees`
table, with its own `password_hash`, in its own database. No SSO, no
shared session, no password sync. The codes look the same because both
systems use the same prefixes for the same people, which is exactly
why "it works in the TMS" is not evidence about the CRM.

An admin reset already exists: Admin → Employees → **Reset password**
(`PUT /api/employees/<id>` with `reset_password`), which issues a
temporary password, clears the lock and the failed-attempt count, and
signs out the old sessions. It is not new and it is not changed.

What was missing is the diagnosis, and one case makes it worth
having: **a deactivated account is refused before the password is
checked**, so it returns the identical message to a wrong password —
and resetting the password will not help. An administrator had no way
to tell those apart, and the obvious fix is the wrong one.

`/login` still answers the same sentence to everybody, on purpose:
telling an anonymous caller which half is wrong tells them which
codes exist. The diagnosis is the other side of the desk — admin
only, read-only, and it never reads, returns or logs a password or a
hash.
