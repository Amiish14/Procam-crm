# CRM-LEAD-FLOW-01 — Simple lead flow, clear lead screen, guided help, and close-out of pending CRM items

**System:** Procam CRM — https://procamlogitech.com/CRM (Flask + PostgreSQL, repo Amiish14/Procam-crm)
**Raised by:** Nilesh Kumar Sinha, 30 Oct 2026
**Build:** Tech team · **Deploy:** Amiish (target: today, 30 Oct 2026) · **Sign-off:** Nilesh
**Priority:** High — users say creating and working a lead has become complicated.

---

## 0. Why this prompt exists

Users are confused at the lead stage. A review of the live All Leads view (300 latest leads) and the lead pop-up on 30 Oct 2026 found:

1. **One thread creates many leads.** Every reply in an email thread becomes a new lead instead of attaching to the open one (Indo Tech Transformers: 4 leads on 19 Oct, 3 on 22 Oct; Hitachi Energy: 2 leads on 29 Oct under two account names; Sarens, Tmilltd, Walchand, Meiden: 2–3 per day).
2. **Junk becomes leads.** Procol (15+), Glafamily (~10), All India Info-Database, Magicbricks, NTT IT Service Desk, Ariba system mails, freight agents offering rates (JCtrans, Winco, Jnclog…).
3. **Blocked clients still get new leads.** Walchandnagar (26 & 29 Oct), Godrej (22, 26, 28 Oct — one at ₹2.25 Cr).
4. **~60 of 300 leads have no owner** (Andritz Hydro, L&T, Siemens Energy, BEML, Linde, Indo-Tech…).
5. **The lead pop-up shows every field for every stage** — about 25 fields in 9 sections. An RFQ-stage lead already shows Quote no., Valid until, Buy rate, Margin, all activity dates and the PIC-change reason. Internal intake scoring ("decided by step_10 · Score: 0 base, 40 1 asks for a price…") is shown to users.
6. **Small bugs:** two "new" stages ("New" and "New Opportunity"); the *Rate sourcing* box shows the customer's own email; the header subtitle reads "Recommend a block or caution for this client" on every lead; Industry and State columns are empty for all leads.
7. **When something is not allowed, the user is only told "no"** — not why, and not what to do next — so they call the Tech team.

A lot of the fix is **already built but switched off** (see §1). This prompt closes those first, then adds what is missing.

### Non-negotiable constraints (carry over from all earlier CRM prompts)
- Keep the current stack: vanilla JS + inline CSS in the existing templates. No framework, no build step, no rewrite.
- Upgrade in place; no existing data lost. **No hard delete** of any lead (CRM-NO-DELETE-01 stays). Junk = soft delete / Not-relevant only.
- Every new behaviour behind a feature flag, default off, switchable from Admin → Switches.
- Every bulk data change: **dry-run report with counts first → Nilesh approves → apply**, with a dated audit remark on every changed record.
- No AI provider or model name anywhere in the UI, mails or logs users see. AI features present as "Procam AI" / "Procam software".
- CRM mails go from **tms@procamgroup.in** with "CRM" in the subject (decision 20 Oct 2026 — there is **no crm@ mailbox**; replace any code or checklist that still waits for crm@).
- Every CRM notification copies the lead's vertical head and Nilesh.
- 4 verticals only: Heavy Transport → James Francis Xavier · Project Freight → Sanjna Vardhan · Installation → Gowdhaman R · Warehousing → Srinivas Marella. No vertical → Nilesh (DIR12010).
- Dropdowns wherever a value can be standardised.

---

## 1. Part A — Close out items already built (do these first, today)

These are coded and pushed but dark. Deploy, run the dry-run, send counts to Nilesh, then switch on.

| # | Tracker ID | What to do | Flag |
|---|---|---|---|
| A1 | crm-mail-ingest-blocked-senders / CRM-JUNK-DELETE-01 | Switch sender block on. Seed the block list with the senders in Appendix B. Then run the junk clean-up dry-run and mail Nilesh the list. | `FEATURE_SENDER_BLOCK` + the 3 junk-delete flags |
| A2 | crm-client-block | Confirm **Walchandnagar Industries Limited** and **Godrej** (all Godrej entities: Godrej, Godrej Enterprises Group, dbpatel@godrej.com) are blocked. Fix the intake path so a blocked client **cannot** create a lead from mail (today it still does — see §2.1). | existing |
| A3 | CRM-LEAD-ACCOUNT-01 | Deploy, switch on, link the BEML "To review" lead to BEML LIMITED, send the dry-run counts (unowned open leads, open leads with no account, unowned on-hold). | `FEATURE_LEAD_ACCOUNT` |
| A4 | CRM-AUTO-ASSIGN-01 | Check the auto-assign is actually firing on new mail leads — 60 of the latest 300 have no owner. Apply the 454-row owner seed if not applied; confirm the digest timer runs. | `FEATURE_AUTO_ASSIGN` |
| A5 | CRM-DECISIONS-01 (part 1) | Merge PR #1, run `scripts/2026_10_21_crm_decisions.py`, install the 2 timers, set tender-desk email / stand-in approver / FF viewer codes on /admin/crm-rules. Send Godrej preview list. | (keep `FEATURE_MARGIN_GATE` off) |
| A6 | crm-autoclose-tasks-closed-leads | Close tasks 1342/1541/1283/1536; send dry-run counts for the bulk close. | — |
| A7 | crm-rfq-chain-01 / crm2-full-build-v2 / ff-comms-01-crm | Replace every "waiting for crm@ mailbox" dependency with tms@procamgroup.in + "CRM" in subject so held clarification mails go out. | — |

Report the result of A1–A7 in the deploy mail (one line each: done / blocked + why).

---

## 2. Part B — Fix lead intake so fewer, cleaner leads reach users

Intake runs automatically on every mail to leads@procamgroup.in (and on manual "+ New Lead"). The user does nothing at this step. Order of checks:

### 2.1 Blocked client gate (fix)
- Match sender email, sender domain, and resolved account (incl. aliases) against the client block list.
- Match → **no lead created**. Mail is stored under *Mail ingest → Blocked client* with the block reason. Vertical head + Nilesh get one daily digest line, not one mail per message.
- Manual "+ New Lead" for a blocked client → refuse with the guided message in §5 (reason, who blocked, how to request un-block).

### 2.2 Sender block / junk gate
- Sender or domain on the block list → *Mail ingest → Blocked* (never deleted, never a lead).
- Classify obvious non-leads into a **Not-relevant queue** instead of the lead list: tender-alert newsletters, marketing/network mails (Glafamily, JCtrans member mails), portal system mails (Ariba, Procol RFX notifications, IT service desks), vendor rate offers from freight agents, Procam's own domains.
- Not-relevant queue is reviewed weekly by the vertical head / admin: one click to *Make it a lead* (then normal flow) or *Confirm not relevant* (optional "block this sender").

### 2.3 Thread de-duplication (new)
Before creating a lead, look for an **open** lead (any stage except Won / Lost / Not Interested / Not relevant) where **any** of these match within the last 30 days:
1. same email thread (Message-ID / In-Reply-To / References), or
2. same normalised subject (strip RE:/FW:/FWD:, client RFQ refs kept) **and** same sender domain, or
3. same client reference number found in subject/body (e.g. `TTL-HL-IN-STD-2026-017`) **and** same account.

Match → **attach** the mail to that lead's email trail, add attachments to its attachment list, raise a "New mail on your lead" notice to the PIC. **Do not create a new lead.**
No match → create the lead.
If the match is uncertain (only rule 2 with a low-similarity subject), create the lead with a yellow **"Possible duplicate of #nnnn"** banner and a one-click *Merge into #nnnn*.

**Merge** (also available from the list via "Mark duplicate"): moves emails, attachments, notes, tasks, quotes and history to the surviving lead; the merged lead is closed as *Duplicate of #nnnn* (not deleted), audited.

### 2.4 Account + owner (automatic)
- Resolve account by domain → GSTIN → company name → learned aliases. Normalise names so "Hitachi Energy", "Hitachienergy", "HITACHI ENERGY INDIA LIMITED" resolve to one account.
- Existing account → owner = account owner (inactive → replacement map → vertical head → DIR12010).
- New account → created as *Account to confirm*, owner = head of the detected vertical, "Owner to confirm" mail (already built in CRM-AUTO-ASSIGN-01).
- **No lead may be left without an owner.** If every rule fails → DIR12010 and a line in the daily digest.

### 2.5 Small intake fixes
- One stage only for a fresh lead: **"New"**. Migrate every "New Opportunity" to "New" (dry-run count first). Remove "New Opportunity" from all dropdowns, filters and reports.
- Fill **Industry** and **State** from the account master; if the account has none, take State from the RFQ origin and leave Industry blank for the owner to pick (dropdown).

---

## 3. Part C — Redesign the lead pop-up (clarity)

### 3.1 Layout
Replace the single long scroll with a fixed **header + stage strip**, a **"What to do now" card**, and **tabs**.

```
┌──────────────────────────────────────────────────────────────┐
│ HITACHI ENERGY INDIA LIMITED          #12345   [High] [RFQ]  │
│ Owner: James Francis Xavier · Heavy Transport · Due 02-Nov   │
│ ● New ─ ● Contacted ─ ◉ RFQ ─ ○ Quoted ─ ○ Negotiation ─ ○ Won│
├──────────────────────────────────────────────────────────────┤
│ WHAT TO DO NOW                                               │
│ Prepare the quote for 2 × 40 MVA transformers,               │
│ Vadodara → Lapanga. Due in 3 days.                           │
│ [ Start rate sourcing ]  [ Ask client a question ]           │
├──────────────────────────────────────────────────────────────┤
│  Summary | Emails (3) | Rate sourcing | Quote | Notes | History│
└──────────────────────────────────────────────────────────────┘
 [Draft email]                        [More ▾]   [Save]
```

- **Header (always visible):** account name, lead no., priority, stage chip, owner, vertical, due/target date, and a red/orange/green due colour (red overdue, orange < 1 day, green ≥ 2 days — the rule already agreed).
- **Stage strip:** clickable; moving forward checks that stage's required fields (§3.3) and opens the guided panel (§5) if anything is missing. Lost / On Hold / Not Interested sit under *More ▾*, not on the main strip.
- **"What to do now" card:** one sentence + at most 2 buttons, driven by stage (§3.3). This is the most important new element.
- **Footer:** keep *Draft email* and *Save*. Move *Close as Lost*, *Mark duplicate*, *Delete – not relevant*, *Change account*, *Change owner*, *Recommend block/caution* into **More ▾** with plain-language labels.
- Remove the header subtitle "Recommend a block or caution for this client"; that action lives in *More ▾*.

### 3.2 Tabs
| Tab | Contents |
|---|---|
| **Summary** | Contact (name, designation, email, phone), Account card with owner, route, cargo, scope chips, Procam AI one-line summary, attachments, original email (.eml), opportunity value + value basis, vertical, PIC + secondary PIC, next follow-up. |
| **Emails** | Customer email trail only (read-only). Count in the tab label. |
| **Rate sourcing** | **Only** supplier requests and replies. Fix the bug where the customer email appears here. Empty state: "No rates asked yet. [Start rate sourcing]". |
| **Quote** | Quote no., value, date, valid until / days, buy rate, margin. **Hidden until stage ≥ RFQ.** Locked fields (margin / buy rate) follow existing visibility rules. |
| **Notes** | Add note + note type; list of notes (never overwritten). |
| **History** | Stage changes, owner/PIC changes with reason, merges, audit. Activity dates (Phone call, Intro mail, Meeting, RFQ) shown here **as a timeline filled automatically** from events (mail sent, meeting logged, stage moved) — not as four empty date boxes the user must fill. |

- The intake-decision text ("Why this is a lead", confidence, signals, score) moves to **History → Intake decision**, visible to Admin only.
- "If changing the PIC, why?" appears **only after** the user changes the PIC dropdown.

### 3.3 What each stage asks for (and the "What to do now" text)
| Stage | Required to move forward | "What to do now" card |
|---|---|---|
| New | Account linked, owner set, vertical set | "Confirm the account and call/mail the client." [Confirm account] [Log a call] |
| Contacted (Call done / Profile sent / Discussion / Appointment / Visit — keep as sub-status dropdown) | One activity logged | "Get the RFQ or requirement from the client." [Log activity] [Ask client a question] |
| RFQ | Scope (cargo, route or site), target date, opportunity value + basis | "Prepare the quote — due {date}." [Start rate sourcing] [Open quote builder] |
| Quoted | Quote no., value, date, validity | "Follow up on quote {no.} — sent {n} days ago." [Draft follow-up] [Log client reply] |
| Negotiation | Latest revised value + note | "Agree final price or revise quote." [Revise quote] [Mark Won] |
| Won | Final value, PO/confirmation attached | "Hand over to operations." [Create handover] |
| Lost / On Hold / Not Interested | Reason from dropdown (5 generic + Others with text); On Hold only by vertical head or director | — |

Note: the main strip shows 6 stages (New, Contacted, RFQ, Quoted, Negotiation, Won). The existing detailed stages (Call Done, Profile Sent, Business Discussion, Appointment, Visit Done) become the **sub-status** of *Contacted*, so reports keep working (map old values, dry-run first).

### 3.4 All Leads list
- Columns: Lead no., Account, Contact, Stage, Owner, Vertical, Value (₹ L), Due (colour), Last activity, Source.
- Rows with possible duplicates show a small "2 similar" link that opens the merge view.
- Filters: default view = **My open leads** for users, **All open leads** for admin. Team dropdown lists **active employees only** (the current list includes inactive staff and shows Pravin Choudhary twice).
- Unassigned leads show a red "No owner" chip — and there should be none after A4/§2.4.

---

## 4. Part D — Manual "+ New Lead" in 2 short steps

**Step 1 — Who:** Account search box (type 3 letters → list with owner). Pick one, or "+ Create new account" inline (name, domain, vertical; owner auto-filled per rules). Blocked accounts show the block message (§5) and cannot be picked. If an open lead already exists for this account in the last 30 days, show it: "Open lead #nnnn exists — Add to it / Create new anyway (reason)".
**Step 2 — What:** Contact (pick or add), vertical (dropdown), short requirement, attach RFQ (optional), target date, rough value (optional). Owner/PIC pre-filled; changeable with reason.
→ Save creates the lead at stage *New* (or *RFQ* if an RFQ file is attached) and opens the new pop-up.

---

## 5. Part E — Guided help: "why not, and what to do now"

**Rule:** No screen may show only an error. Every refusal, empty state, failed action or validation message must give three things:

1. **What happened** — one plain sentence.
2. **Why** — the rule, in plain words (no codes, no stack traces).
3. **What to do** — numbered steps, with **buttons that take the user there** (open the tab, focus the field, open the account, draft the mail to the person who can approve).

Example message pattern:
> **You can't move this lead to Quoted yet.**
> A quote number and quote value are needed first.
> 1. Open the **Quote** tab → [Take me there]
> 2. Enter Quote no. and Value, then Save.
> Still stuck? [Ask for help]

### 5.1 Build it as one component
- One JS helper (e.g. `showGuide(code, context)`) and one server-side catalogue table `crm_help_guides` (code, title, why, steps[], actions[], audience, active, last_edited_by). Admin can edit wording from **Admin → Help texts** without a deploy.
- Every existing refusal / validation in the lead, account, quote and RFQ flows is routed through this helper. Minimum catalogue at go-live:

| Code | When | Guided steps (summary) |
|---|---|---|
| CLIENT_BLOCKED | Lead/quote for a blocked client | Who blocked + reason; [Request review] mails vertical head + Nilesh |
| NO_ACCOUNT | Save without account | [Link account] / [Create account] |
| NO_OWNER | Lead without owner | Auto-fix offer: [Assign to account owner {name}] |
| NOT_YOUR_LEAD | User edits someone else's lead | Who owns it; [Ask owner to add me as secondary PIC] |
| STAGE_FIELDS_MISSING | Stage move blocked | Lists the missing fields with [Take me there] per field |
| HOLD_NOT_ALLOWED | User tries On Hold | Only vertical head/director; [Request hold from {head}] |
| POSSIBLE_DUPLICATE | Duplicate found | [Open #nnnn] [Merge] [Keep both – give reason] |
| DELETE_NOT_ALLOWED | Delete on a lead with activity | Explain no-delete rule; [Close as Lost instead] |
| MAIL_NOT_SENT | Outgoing mail failed / held | Plain reason (e.g. external mail held); what will happen next; [Retry] |
| RATE_SOURCING_EMPTY | No supplier asked | [Start rate sourcing] |
| QUOTE_EXPIRED | Validity passed | [Revise quote] [Extend validity] |
| SESSION_EXPIRED / NO_PERMISSION | Login/role | Who grants the role; [Request access] |

### 5.2 Help everywhere
- A small **"?"** on each tab and each stage opens a 3-line "How this works" note with a [Show me] that highlights the next control (simple CSS outline + scroll; no tour library).
- **"Ask for help"** button: opens a short form pre-filled with page, lead no., user, last error code and a screenshot-free description field; creates a ticket in the existing ticket register (TMS bug register / support ticket module) and replies with the ticket number. Copies the vertical head only if the issue blocks a client response.
- First time a user opens the new pop-up: one dismissible banner "The lead screen has changed — [See what's new]" (3 bullets). Remember dismissal per user in the database, not browser storage.
- Ask Procam AI (already on screen, Ctrl K): feed it the help catalogue + the current lead's stage and missing fields, so "why can't I save?" gets the same guided answer.

### 5.3 Measure it
- Log every guide shown (code, user, lead, clicked action Y/N). Weekly table on the Thursday pack: top 10 guides shown, % resolved by the user without a ticket. Goal: fewer "how do I" calls to the Tech team.

---

## 6. Part F — One-time data clean-up (dry-run first)

Run read-only reports, mail the counts and lists (Excel) to Nilesh, apply only after approval, audit every change.

1. **Duplicates:** groups by §2.3 rules over all open leads → proposed survivor (earliest lead with most activity) + merge list.
2. **Junk / not relevant:** leads from the Appendix B senders and the §2.2 classes with no activity → soft delete as not relevant (activity present → Close as Lost "Not relevant / Others").
3. **Blocked clients:** open leads for Walchandnagar and Godrej entities → Close as Lost "Client blocked"; list to Nilesh first (Godrej has a ₹2.25 Cr lead open).
4. **No owner:** apply §2.4 rules → proposed owner per lead.
5. **Account names:** proposed merges of name variants (Hitachi Energy, Indo Tech/Indo-Tech/Indo Tech Transformers, Walchand/Walchandnagar, Sarens/Sarens Heavy Lift, Andritz variants, Gevernova, Tmilltd, Hacklin variants, Hyup-Jin variants, Praj, BHEL/Bhel, Sanghvi Movers/Sangreen) — Seema reviews client-name mapping before apply.
6. **Stage:** "New Opportunity" → "New"; old detailed stages → Contacted + sub-status.
7. **Industry / State** back-fill from account master.
8. Rule from earlier clean-up stays: leads older than 4 months still open/on-hold → Lost "No Response"; never delete.

---

## 7. Acceptance tests (must pass before switching flags on)

1. Reply to an existing RFQ thread → mail attaches to the open lead; no new lead; PIC notified.
2. Mail from walchand.com or godrej.com → no lead; appears under Mail ingest → Blocked client.
3. Mail from partner@procol.in / glafamily.com → Blocked or Not-relevant queue; no lead.
4. New mail from an unknown company → lead created with an owner (never blank) and account *to confirm*.
5. Lead at RFQ stage → Quote tab visible; at New stage → Quote tab hidden.
6. Rate sourcing tab shows only supplier mails; customer RFQ mail never appears there.
7. Trying to move to Quoted without quote no. → guided message with [Take me there] that focuses the Quote no. field.
8. User tries On Hold → HOLD_NOT_ALLOWED guide with [Request hold from {head}].
9. No "New Opportunity" stage anywhere; no AI provider name anywhere; intake scoring visible to Admin only.
10. Header subtitle no longer says "Recommend a block or caution"; that action works from More ▾.
11. Team filter shows active employees only, no duplicate names.
12. "Ask for help" creates a ticket and shows the ticket number.
13. Existing test suite passes; new tests added for dedupe, block gate, stage gating, guide catalogue.

---

## 8. Deliverables and roll-out

- Code behind flags: `FEATURE_THREAD_DEDUPE`, `FEATURE_BLOCKED_CLIENT_GATE`, `FEATURE_NOT_RELEVANT_QUEUE`, `FEATURE_LEAD_POPUP_V2`, `FEATURE_GUIDED_HELP`, `FEATURE_STAGE_SIMPLIFY`.
- Migration for `crm_help_guides` + seed catalogue (§5.1), stage mapping, merge table.
- Dry-run scripts for §6 (read-only by default, `--apply` only after approval).
- Deploy mail to Nilesh (cc vertical heads not needed yet) with: Part A status table, dry-run counts, flags switched, anything blocked and why.
- Roll-out: switch on for Nilesh + the 5 RFQ-chain pilot users first, then all users after one working day without issues.
- Update the build prompt tracker row CRM-LEAD-FLOW-01 with commits, test count and next action.

---

## Appendix A — User guide text (seed for "See what's new" and the help notes)

**How a lead works now**
1. Leads arrive from leads@procamgroup.in. The system already links the account, sets the owner and joins replies to the same lead. You don't need to create leads for client replies.
2. Open your lead. Read the **What to do now** card at the top and click its button.
3. Move the lead along the stage strip: New → Contacted → RFQ → Quoted → Negotiation → Won. If something is missing, the screen tells you what and takes you there.
4. Customer mails are under **Emails**. Supplier rates are under **Rate sourcing**. The quote is under **Quote** (appears from RFQ stage).
5. Wrong account, wrong owner, duplicate, not relevant, lost? Use **More ▾**.
6. Create a lead by hand only when the system did not: **+ New Lead** → pick the account → describe the requirement → Save.
7. Stuck? Press **?** on the screen or **Ask for help** — you get a ticket number and the Tech team follows up.

## Appendix B — Senders seen creating junk leads (seed the block / not-relevant list; Nilesh to confirm)
partner@procol.in (Procol RFX notifications) · *@glafamily.com · tnis.s117@gmail.com, tendernews.shippers@gmail.com (All India Info-Database) · info@magicbricks.com · nttin.operations.information@global.ntt · *@ansmtp.ariba.com (route to the Jindal lead by reference, never as a new lead) · system@sarjak.com · info@procurengine.com · nikusi@gmail.com · freight-agent rate offers (*@jctrans.net, *@wincologistics.com, *@jnclog.cn, *@kstopway.com) → Not-relevant queue, not block, because some may be genuine partners.
