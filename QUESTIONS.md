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
