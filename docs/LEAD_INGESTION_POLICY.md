# Lead Ingestion — one inbox, classify before creating

_v2026-09-12. Supersedes v2026-09-01, which described the
capture-everything policy this engine replaced._

```
Employee receives a lead
        ↓ forwards it
leads@procamgroup.in
        ↓ Graph webhook → /api/email/webhook
CRM unwraps the forward and pulls out the ORIGINAL prospect
        ↓
Intake engine classifies it  ──► not a lead? filed with a reason,
        ↓ it is a lead              nothing deleted
Account resolved → two PICs assigned → vertical recommended
        ↓
Lead row (source=email, stage="New Opportunity")
```

## 1. Classify first, then create

**This section was rewritten on 2026-09-12 and reverses the previous
policy.** Until then every message that arrived became a Lead. Against
the real mailbox that produced 2,076 leads from 2,076 messages, of which
roughly three quarters were replies, forwards of enquiries already held,
internal mail, supplier quotes and newsletters.

The engine now decides what a message is *before* a lead exists. Ten
outcomes, in [`app/services/lead_intake.py`](../app/services/lead_intake.py):

| | Outcome | What happens |
|---|---|---|
| A | New lead | Lead created, account resolved, two PICs assigned |
| B | Existing lead communication | Appended to the lead it belongs to |
| C | Reply to an existing RFQ | Appended |
| D | Internal Procam email | Filed, no lead |
| E | Rate sourcing / vendor | Filed to the Rate Sourcing tab |
| F | Forward of an existing RFQ | Appended |
| G | Quote submission | Filed, reference and amount extracted |
| H | Duplicate | Held against the lead it may duplicate |
| I | Non-business | Filed |
| J | Needs review | Waits for a person in Lead Review |

The tree is **pure** — every database lookup goes through a `Context`
object — so it is tested against dictionaries with no database at all.

### Nothing is dropped and nothing is deleted

A message that does not become a lead is still recorded in full, as an
`EmailClassification` row with the reason it was filed that way. That is
both the audit trail and the training set. A wrong decision is one click
to reverse in Lead Review, and the reversal is itself recorded.

### Measured effect

Against the live mailbox: **2,076 messages → 493 leads**, a 76% reduction
in noise, with the review queue settling at 59 items. The dry run that
produced those numbers is `scripts/classify_mailbox_dryrun.py`, and it is
read-only.

### The order matters

Steps 1 to 9 are deterministic facts — a thread match, a Procam sender, a
known supplier domain, the logistics gate. Only step 10 is a judgement,
and only step 10 has a confidence score. An email with no body and no
attachment cannot reach "new lead" at all, however its subject scores:
that rule exists because a customer forwarded our own company profile
back to us and it scored 62 on the words in our own tagline.

### Where a model is involved, and where it is not

Phase 4 consults an LLM at **step 10 only**, and only when the score
lands between 25 and 79. It cannot overturn a thread match, a Procam
sender, a vendor match or the logistics gate; it cannot name a lead to
attach to; and an opinion under 70% confidence is recorded but not
applied. It is off unless `LEAD_INTAKE_AI=on` and a key are both set.
Every opinion is stored beside the rule's own answer so the two can be
compared. See [`lead_intake_ai.py`](../app/services/lead_intake_ai.py).

### Screens

| Screen | Who | What it is for |
|---|---|---|
| `/lead-review` | Sales + Admin | The queue. Accept, reject with a reason, or reclassify |
| `/accounts/owners` | Admin | Two PICs per account — the mapping that makes assignment automatic |
| `/intake-intelligence` | Admin | Accuracy, what the corrections suggest, what the model said |
| `/triage` | Admin | Lead triage across the whole funnel |

## 2. One inbox, and only one

`leads@procamgroup.in` is the sole source of leads. No other mailbox,
inbox or email account is connected. Enforced in four places, so no single
misconfiguration can open a second one:

| Where | Guard |
|---|---|
| [service.py](../email_ingest/service.py) | `crm_inbox_email()` is the single authority for the address; falls back to the canonical value when the env var is missing |
| [subscription.py](../email_ingest/subscription.py) | `create()` refuses to subscribe to any mailbox other than the sanctioned one |
| [webhook.py](../email_ingest/webhook.py) | Every notification must be for the sanctioned mailbox — see below. Rejections are logged as `EmailEvent(status='rejected')` |
| [pipeline.py](../email_ingest/pipeline.py) | The retired poll path refuses to run when `EMAIL_INGEST_MAILBOX` disagrees with `CRM_INBOX_EMAIL` |

To verify what is actually connected:

```
GET /api/email/subscriptions      # admin — lists live Graph subscriptions
```

### How the webhook checks the mailbox

Graph does **not** name the mailbox in a notification the way the
subscription was created. It sends either form, with varying case and
sometimes no leading slash:

```
/users/leads@procamgroup.in/mailFolders('Inbox')/messages   ← UPN
Users/9a0b6a4e-0423-4e71-94b3-a77b849f2d05/Messages/AAMk…   ← object id
```

So the check is two-stage:

1. **Cheap:** parse the user segment out of `resource` and compare it
   against both the mailbox's UPN and its directory object id (resolved
   once via `GET /users/{upn}?$select=id`, then cached).
2. **Authoritative:** fetch the message scoped to the sanctioned mailbox
   (`/users/leads@procamgroup.in/messages/{id}`). Graph returns 404 for a
   message id belonging to any other mailbox, so a successful fetch
   *proves* the message is in the leads inbox.

A stage-1 mismatch is only logged, never a rejection on its own — the
fetch decides. Rejection requires both to fail.

> **2026-09-02 outage.** An earlier version did stage 1 only, matching the
> UPN as a substring. Because Graph sends the object-id form, *every*
> notification was rejected and no leads were ingested at all. Fixed, with
> a regression test in `tests/test_webhook_mailbox_lockdown.py`. Events
> lost in that window are recoverable with
> `scripts/2026_09_02_replay_rejected_events.py`.

## 3. The forward is a container; the lead is inside it

This is the part that *does* transform the message.
[`parser.analyze_forward()`](../email_ingest/parser.py) splits a forwarded
email into three parts:

| Part | Where it goes |
|---|---|
| The employee's covering note | `notes` header + `opp_notes.forward_note` — labelled, never parsed as lead content |
| The original headers (`From:` / `Subject:` / `Sent:` / `To:`) | The prospect's identity and the original subject |
| The original body | `notes`, and the input to signal extraction and both AI extractors |

`msg["from"]` is then rewritten in place to the **original external
sender**, so every downstream step — dedup, AI extraction, Lead
attribution — sees the customer.

Result: the prospect's name, email, phone, company and requirement all
come from the original message. A phone number in the employee's own
covering note cannot leak into the lead's phone field.

Forward shapes handled: Outlook desktop/web (`---------- Forwarded
message ----------` and HTML `<b>From:</b>` header blocks), Gmail, Apple
Mail (`Begin forwarded message:`), plain-text (`-----Original
Message-----`), and mobile (`On <date>, X <a@b.c> wrote:`).

### The forwarding employee is never the contact

If the original sender cannot be resolved, the Lead is **still created** —
but the contact fields are left blank and `needs_review` is set, so a
human fills in the prospect from the body. The employee is never used as
a fallback. Three guards back this up:

* an internal-to-internal thread resolves to no external sender, so the
  contact is blanked rather than set to either Procam address;
* [`enrich.py`](../email_ingest/enrich.py) strips the forwarder's address,
  name, and domain-derived company back off the contact fields if the AI
  or a signature block put them there;
* both AI extractors are told never to return a `procamgroup.in` /
  `procamlogistics.com` address as the customer, and the router nulls one
  out if it appears anyway.

A bare `From:` line is deliberately *not* treated as proof of a forward —
an ordinary quoted reply chain looks identical, and a prospect replying
into the inbox would otherwise have their quoted counterparty promoted
over themselves. An explicit forward marker, a `Fw:`/`Fwd:` subject, or an
internal sender is what marks a forward.

Who forwarded it is preserved as provenance in `opp_notes.forwarded_by`
and in the `[Forwarded to CRM by …]` line at the top of the lead notes.

## 4. Visibility

Every notification is recorded on the **Email Inbox** admin page,
whatever the outcome:

```
GET  /api/email/inbox                    # ?status=lead_created | skipped | failed | rejected
GET  /api/email/inbox/<id>               # full payload
POST /api/email/inbox/<id>/retry         # re-process (idempotent)
POST /api/email/inbox/<id>/retry?upgrade=1   # re-run the enricher over an existing Lead
```

## 5. The CRM format is unchanged

`Lead(source='email', stage='New Opportunity', …)` is built by the same
`enrich.build_enriched_lead_kwargs()` both ingest routes already shared,
so the "LEAD SUMMARY · FROM INBOUND EMAIL" card renders exactly as before.

## Configuration

| Env var | Value | Effect |
|---|---|---|
| `CRM_INBOX_EMAIL` | `leads@procamgroup.in` | The one sanctioned lead source |
| `EMAIL_INGESTION_MODE` | `mailbox` | Webhook-driven, real-time ingestion |
| `EMAIL_WEBHOOK_SECRET` | random string | Graph echoes it on every notification |
| `EMAIL_INGEST_SKIP_DOMAINS` | `procamlogistics.com,procamgroup.in` | Which domains count as "internal" for forward unwrapping — **not** a lead filter |

## Which path actually runs

Production ingests through
[`email_ingest/single_message.py`](../email_ingest/single_message.py) —
`process_single_message()` — which both the webhook and the poll call.
That is where the intake engine is wired in. `pipeline.py` is the older
batch path and is **not** what runs; a change made only there has no
effect on live mail.

## Known difference: the retired poll path

[pipeline.py](../email_ingest/pipeline.py) — the old 09:00 IST poll — is
disabled (`EMAIL_INGESTION_MODE=mailbox`) and kept only for one-off
historical backfills. It still has its original skip/dedup behaviour, so a
backfill run would filter where the live webhook does not. If you ever
need a full historical import of the mailbox, say so and it can be
switched to capture-everything too.

## Tests

```
python3 tests/test_forwarded_leads.py
python3 tests/test_webhook_mailbox_lockdown.py
```

Covers: Outlook/Gmail/Apple Mail/HTML forward shapes, original-sender
extraction, provenance capture, direct (non-forwarded) mail, newsletters
and auto-replies being captured with triage tags, unresolvable forwards,
internal-only threads, and quoted reply chains.
