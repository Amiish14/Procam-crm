# Lead Ingestion — one inbox, capture everything

_v2026-09-01._

```
Employee receives a lead
        ↓ forwards it
leads@procamgroup.in
        ↓ Graph webhook → /api/email/webhook
CRM unwraps the forward and pulls out the ORIGINAL prospect
        ↓
Lead row (source=email, stage="New Opportunity")
```

## 1. The mailbox is the filter

`leads@procamgroup.in` exists for exactly one purpose: employees forward
leads into it. So **every message that arrives becomes a Lead**. There is
no content filtering, no confidence threshold, no junk detection, no
"is this really a lead?" check. If it reached the inbox, someone decided
it belongs in the CRM, and silently dropping mail loses business.

The only message ever skipped is one that was **already ingested**
(deduped on `internetMessageId`, so a webhook retry cannot double-insert).

Anything the parser considers unusual — an auto-reply, a bulk sender, a
message with no cargo/route/RFQ signal — is recorded as a **`triage_tag`**
in the Lead's `opp_notes` and the Lead is created regardless. The tag is a
label for sorting in the UI, never a reason to reject.

## 2. One inbox, and only one

`leads@procamgroup.in` is the sole source of leads. No other mailbox,
inbox or email account is connected. Enforced in four places, so no single
misconfiguration can open a second one:

| Where | Guard |
|---|---|
| [service.py](../email_ingest/service.py) | `crm_inbox_email()` is the single authority for the address; falls back to the canonical value when the env var is missing |
| [subscription.py](../email_ingest/subscription.py) | `create()` refuses to subscribe to any mailbox other than the sanctioned one |
| [webhook.py](../email_ingest/webhook.py) | Every notification's `resource` must name the sanctioned mailbox, checked *before* any Graph fetch. Rejections are logged as `EmailEvent(status='rejected')` |
| [pipeline.py](../email_ingest/pipeline.py) | The retired poll path refuses to run when `EMAIL_INGEST_MAILBOX` disagrees with `CRM_INBOX_EMAIL` |

To verify what is actually connected:

```
GET /api/email/subscriptions      # admin — lists live Graph subscriptions
```

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
```

Covers: Outlook/Gmail/Apple Mail/HTML forward shapes, original-sender
extraction, provenance capture, direct (non-forwarded) mail, newsletters
and auto-replies being captured with triage tags, unresolvable forwards,
internal-only threads, and quoted reply chains.
