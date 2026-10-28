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
