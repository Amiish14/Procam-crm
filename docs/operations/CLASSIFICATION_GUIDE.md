# Classification Guide

For CRM administrators who review intake mail, maintain the Vendor
Master, or need to explain why an email did or did not become a lead.
Pages are under `https://procamlogitech.com/CRM`.

| Page | Permission | Use |
|---|---|---|
| **/CRM/lead-review** | Lead Triage (`admin.triage`) | Act on what the classifier could not decide; overrule duplicates |
| **/CRM/intake-intelligence** | Lead Triage | Accuracy, corrections, rules proposed from corrections |
| **/CRM/intake/vendors** | Master Data (`admin.master`) | Vendor Master — domains whose mail is rate sourcing |
| **/CRM/accounts/owners** | Lead Triage | Account owners and account email domains |

Every email the leads mailbox receives is classified **before** anything
is created, and every decision is recorded (`email_classifications`),
whether or not anyone looks at it. The kill switch `LEAD_INTAKE_MODE`
(`enforce` / `observe` / `off`) is described in the
[AI Configuration Guide](AI_CONFIGURATION_GUIDE.md).

## The ten classes

Only **New Lead / RFQ** creates a lead. Everything else is filed against
the lead it belongs to, or set aside — nothing is deleted.

| Class | Meaning | Effect |
|---|---|---|
| **A — New Lead / RFQ** | A new enquiry | Lead created and assigned to the account's owners |
| **B — Existing Lead Communication** | Part of a conversation we already hold | Added to that lead's email trail |
| **C — Internal Communication** | A Procam address, mailbox only copied in | Set aside |
| **D — Duplicate** | The same enquiry as an existing lead | Added to that lead's trail; no new lead |
| **E — Reply / Follow-up** | A reply on a known thread | Added to the trail |
| **F — Forward of Existing** | A forward of a known thread | Added to the trail |
| **G — Vendor / Rate Sourcing** | A supplier's rates, or us asking a supplier | Added to the lead's rate-sourcing tab when one matches |
| **H — Quote Submission** | A quotation going out, or an agent quoting us | Added to the trail |
| **I — Non-business** | Newsletters, notifications, nothing logistics | Set aside |
| **J — Needs Admin Review** | The classifier would not guess | Waits in Lead Review |

## How a decision is made

The checks run in this order and the first that applies decides. The
step is recorded (`step_3` … `step_10`) and shown in the review screen's
**Classifier explanation** panel.

| Step | Check | Decides |
|---|---|---|
| 3 | Conversation id, In-Reply-To or References match a lead or its trail | B, E or F — final, nothing overrides it |
| 4 | Sent by a Procam address | H if it quotes, G if it asks a supplier for rates, B if the subject matches a lead, otherwise C |
| 5 | `RE:` / `FW:` with no thread match | E or F if subject and counterparty match a lead, otherwise J |
| 6 | Sender's domain is in the **Vendor Master** | G |
| 7 | An inbound quotation matching a known enquiry | H |
| 8 | No cargo, RFQ, route or contact signal | I |
| 9 | **Duplicate score** — see below | D at 70 or more; J from 31 to 69 |
| 10 | **Confidence score** | I below 25; J from 25 to 49 (or when the message is empty); A at 50 or more |

A forward a colleague relayed is judged by the **customer inside it**, not
the colleague — the explanation panel says "forward unwrapped".

When the model second opinion is switched on it is asked **only at step
10**, only for scores from 25 to 79, and only overrides the rule when it
is at least 70% sure. Its answer is stored beside the rule's (`ai_model`,
`ai_class`, `ai_confidence`, `ai_reason`, `ai_applied`) and shown in the
explanation panel.

### Confidence (step 10)

A base of 40, plus or minus named contributions: asks for a price (+25),
logistics vocabulary (+15), a route (+10), an enquiry reference (+10), a
phone number (+5), an attachment (+5); a reply (−30), sent by us (−25),
a supplier domain (−15), newsletter wording (−40), and so on. The score
is kept between 0 and 100. Each contribution is stored and listed in the
**Why this score** panel, with the enquiry words that were found.

## Duplicate detection (step 9)

The message is compared with each of the **500 most recent leads created
in the last 30 days** that have a sender address. The lead with the
highest total is the match. Weights add up and are capped at 100.

| Signal | Weight | Fires when |
|---|---|---|
| `same_thread` | 100 | The conversation id, In-Reply-To / References, or the message id itself names the lead or an email on its trail. Checked even when the lead is older than 30 days. |
| `same_reference` | 70 | The same enquiry reference appears in both — an RFQ, RFP, tender, enquiry, NIT or "ref" number, in the subject, body, trail subjects or attachment names. Separators are ignored (`NTPC/2026/1234` = `ntpc-2026-1234`). A reference must contain a digit; a bare year ("RFQ 2026") or a date is not one. A **short or all-digit** reference ("RFQ 4471") only counts when both come from the **same account**. |
| `same_sender_and_subject` | 55 | The sender is one of the lead's addresses (email, second email, original sender) **and** the subject is the same once `RE:`/`FW:` and status notes ("– Reminder", "– Not Quoted") are removed. A subject under 6 letters ("RFQ") is not evidence. |
| `same_account_and_route` | 35 | Same account **and** the same origin **and** destination. The lead's route comes from its extracted summary, or from its original email; places are compared by city ("Vadodara, Gujarat" = "Vadodara plant"). |
| `same_attachment_name` | 30 | An attachment with the same file name (case ignored). Ignored: inline images and logos (`image001.png`, `Outlook-….png`), signed-mail and calendar parts (`.p7s`, `.ics`, `.vcf`, `.dat`), and names made only of generic words and numbers (`RFQ.xlsx`, `Scan_20260901.pdf`). |
| `same_account_in_window` | 20 | Same account inside the 30 days. |
| `same_cargo` | 15 | Every distinctive word of the lead's extracted cargo description ("reactor vessel", not "heavy project cargo") appears in the message **and** both state the same weight (180 MT = 180,000 kg). Shipment dates are not compared: the CRM does not hold a reliable shipment date for a new message. |

**Same account** means: the same sender address, or the same account in
the Account Master (including its other email domains), or the same
company domain. Two senders on free mail (gmail.com and similar) are
**not** one account.

### What the totals mean

| Example | Signals | Score | Result |
|---|---|---|---|
| The same tender forwarded by a consultant | reference | 70 | Duplicate |
| A customer re-sends their enquiry with "– Reminder" | sender+subject, account | 75 | Duplicate |
| A colleague at the same customer sends the same drawing for the same route | route, attachment, account | 85 | Duplicate |
| Same customer, same route, new drawing | route, account | 55 | Needs Review |
| Same customer, different route, different tender | account | 20 | Not a duplicate |
| Two customers both attach `RFQ.xlsx` | — | 0 | Not a duplicate |

The reasons that fired are stored with the decision and shown in **Why
this score**, each with its weight and the matched lead as a link.

## Vendor Master

**/CRM/intake/vendors** (Master Data permission). Mail from a listed
domain is filed as **Vendor / Rate Sourcing** at step 6 and never creates
a lead.

- **Subdomains are covered.** `maersk.com` also covers `mail.maersk.com`
  and `in.mail.maersk.com`. It never covers `notmaersk.com` — matching is
  by whole domain labels.
- **Categories:** Supplier, Shipping Line, Transporter, CHA (customs
  house agent), Warehouse Partner, Airline, Overseas Agent, Other. Older
  rows typed by hand keep their stored text (shown under the category);
  anything unrecognised is shown as Other. The category is for people —
  every active row has the same effect on classification.
- **Adding:** enter the bare domain. Capitals, a leading `@` and `www.`
  are removed for you. Refused: an email address, a web address, spaces,
  anything without a dot, a public suffix (`co.in`), and free-mail
  domains (`gmail.com`) — registering those would stop customers'
  enquiries becoming leads. A domain already listed (active or not) is
  refused; edit or reactivate that row.
- **Editing:** category, company name and notes. The domain itself cannot
  be changed — add the new domain as its own row.
- **Deactivate, never delete.** A deactivated row stops matching at once
  and keeps who added it, when and why. Give a reason when deactivating;
  it goes into the audit trail (**/CRM/admin/audit**, action
  `config.vendor_change`). Reactivate to restore it.
- **How it got here:** "Added by hand", or "Learned from review
  rejections" with the number of rejections the proposal was based on.

**Learned vendors.** When the same domain is rejected three times with a
vendor reason (Vendor / Shipping Line / Transporter Rate Sourcing, Vendor
/ Supplier Communication), Intake Intelligence proposes it. Choose the
category beside the proposal and press **Apply**; with no choice it is
filed as Other. Nothing is ever added without someone pressing Apply.

## Lead Review actions and what they teach

Each action writes a correction beside the original decision — the pair
is the training example. Nothing is deleted.

| Action | What happens | Recorded as | What learning does with it |
|---|---|---|---|
| **Accept as lead** | Lead created from the stored email and assigned to the account's owners | corrected to New Lead, reason "Accepted at review" | Counted as a missed enquiry rescued (if the engine said something definite) or a review resolved as a lead |
| **Not a duplicate — create lead** | As Accept. The email stays on the trail of the lead it was wrongly filed against; the classification keeps that lead's number | corrected to New Lead, reason "Not a duplicate" | Counted as a missed enquiry. Three or more from step 9 appear in Intelligence as "step_9 called … Duplicate when they were New Lead" — the duplicate rule needs looking at |
| **Merge** (lead number) | Email added to that lead's trail | corrected to Existing Lead Communication | Counted as a correction of the class |
| **Reject** (reason) | Nothing created | original class kept, with the reason | Three rejections of one domain with a vendor reason → vendor proposal; with Spam / Job Application / Test → blocklist proposal. A rejected New Lead counts as a false positive |
| **Mark duplicate / internal / quote / rate sourcing** | Nothing created | corrected to that class | Wording that recurs in mail marked quote or rate sourcing is proposed as a phrase rule (added by a developer, never automatically); a step overruled three times the same way is reported |
| **Reassign** (reason) | Lead moved to other owners | assignment history | Three moves of one account's leads away from its default owner → owner proposal |

**Held as duplicates** (tab on Lead Review) lists mail filed as a
duplicate in the last 30 days that nobody has checked. A confident
duplicate never waits in the main queue — it is filed straight against
its lead — so this tab is the only place to catch a wrong one. Confirming
it with **Mark duplicate**, or overruling it with **Not a duplicate**,
removes it from the tab.

**History** (panel on each item) lists earlier decisions for the same
sender — including the customer behind a relayed forward — and the same
conversation, newest first, up to 20, with what a person changed each
to. Use it to see whether this sender has been corrected before.

## Rollout notes

The Vendor Master adds four nullable columns to `vendor_domains`
(`name`, `notes`, `updated_at`, `updated_by`). Vendor matching during
classification and the list of Intelligence proposals keep working on a
database that does not have them yet; the Vendor Master screen and
applying a vendor proposal need them, so add the columns before or with
the deploy.
