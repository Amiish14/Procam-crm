# CRM ↔ TMS integration contract

Written for whoever builds the TMS side, in **Procam LR Main**. The
CRM half described here is built and tested; nothing in this document
asks you to change the CRM.

Two applications, two databases. Neither reads the other's tables.
Everything crosses an authenticated HTTP boundary.

---

## 1. Base and authentication

```
Base URL   https://procamlogitech.com/CRM/api/integration/v1
Header     Authorization: Bearer <token>
```

The token is issued by the CRM administrator as a `name:token` pair in
the CRM's `CRM_INTEGRATION_TOKENS`. The name identifies the calling
system and appears in the CRM's integration log, so give each system
its own.

There is **no session fallback**. A browser cookie will not work here,
on purpose: a person's session must not be usable as a system
credential.

Start by proving the token:

```
GET /ping
→ 200 {"ok": true, "service": "procam-crm", "caller": "procam-tms",
       "request_id": "…"}
```

---

## 2. Every request and every response

Send these headers on anything that writes:

| Header | Why |
|---|---|
| `Authorization: Bearer <token>` | required on every call |
| `Idempotency-Key: <your key>` | **send one.** See §3 |
| `X-Request-Id: <your id>` | optional; the CRM echoes it and logs it |

Every response carries `request_id`. Quote it in any question about a
call.

Errors are always the same shape, so branch on `code` and never on the
prose:

```json
{"ok": false,
 "error": {"code": "client_blocked", "message": "…"},
 "request_id": "…"}
```

| code | status | meaning |
|---|---|---|
| `unauthorized` | 401 | bad or missing token |
| `integration_disabled` | 503 | no tokens configured on the CRM |
| `missing_name` / `missing_company` / `missing_query` | 400 | a required field |
| `missing_crm_id` / `missing_tms_id` | 400 | a link needs both sides |
| `not_found` | 404 | no such CRM record |
| `client_blocked` | 403 | **see §6** |

---

## 3. Idempotency — read this one

The CRM cannot tell a retry from a second request unless you say so.
A call that times out may well have been applied, so **send an
`Idempotency-Key` on every POST**, and send the same key when you
retry.

- Same key, same endpoint, within 48 hours → the CRM returns the
  original response with `"replayed": true`, and does nothing again.
- No key → a second POST is a second request, and you get two leads.
  The CRM does not guess at intent.

A good key is something stable from your side: `tms-project-4471` or
`job-88213-create-lead`.

---

## 4. What the TMS can call

### Accounts

```
GET  /accounts?name=…&gstin=…&domain=…
→ {"found": true, "account": {"crm_account_id": 42, "name": "…", …}}
```

Name matching tolerates Ltd / Limited / Pvt and punctuation, so send
the name as you hold it.

```
POST /accounts            {"name": "...", "city": "...", "email": "..."}
→ 201 {"created": true,  "account": {…}}
→ 200 {"created": false, "account": {…}}      ← already existed
```

Find-or-create. The CRM will not make a second account for a name it
already holds.

### Leads

```
GET  /leads?crm_lead_id=…
GET  /leads?tms_project_id=…        ← resolves through the link table
GET  /leads?company=…
GET  /leads/<crm_lead_id>
→ {"found": true, "lead": {"crm_lead_id": 101, "company": "…",
                            "stage": "…", "links": [...]}}
```

```
POST /leads
{"company": "...",            ← required
 "project": "...", "contact_name": "...", "email": "...",
 "phone": "...", "city": "...", "assigned_to": "<CRM emp_code>",
 "tms_project_id": "...",     ← optional; links in the same call
 "notes": "..."}
→ 201 {"created": true, "lead": {…}, "link": {…}}
```

For work that reached the TMS first. The result is an ordinary CRM
lead — on the board, owned by somebody — not a shadow record.

### Linking

```
POST /leads/<crm_lead_id>/link-tms
{"tms_project_id": "TMS-PRJ-77", "tms_job_id": "...",
 "link_type": "project"}      ← project | job | client
→ 201 {"created": true,  "link": {…}}
→ 200 {"created": false, "link": {…}}      ← already linked
```

Linking the same pair twice makes one link.

---

## 5. What the CRM will send you

Set `TMS_WEBHOOK_URL` and `TMS_WEBHOOK_TOKEN` in the CRM's
environment and the CRM will POST to that URL:

```json
{"event": "lead.won",
 "event_id": "lead.won:1042",
 "request_id": "…",
 "sent_at": "2026-10-06T04:12:00Z",
 "source": "procam-crm",
 "attempt": 1,
 "data": { … }}
```

Headers: `Authorization: Bearer <TMS_WEBHOOK_TOKEN>`, `X-Request-Id`,
`X-Event-Id`, and `Idempotency-Key`.

| event | data |
|---|---|
| `lead.won` | `crm_lead_id`, `crm_account_id`, `company`, `project`, `value`, `assigned_to` |
| `quote.won` | `crm_quote_id`, `crm_lead_id`, `crm_account_id`, `quote_number`, `value`, `currency` |

### Delivery is now durable

**This changed on 2026-10-14, and the change is the important part of
this document.** The CRM used to POST inline and log the failure. You
pointed out that you cannot discover a missed `lead.won` by polling —
a deal that was won and never announced looks identical to one that
was always won — and you were right. "Poll as well as listen" was
advice that does not work.

Now the event is written to `webhook_outbox` inside the transaction
that caused it, and a worker on a two-minute systemd timer delivers
it. **The CRM will keep trying until you accept it.**

### What you must do

**Deduplicate on `Idempotency-Key`** — it is also `event_id` and the
`X-Event-Id` header. All three carry the same value, which is stable
for the life of the event and identical on every retry. The format is
`<event>:<crm object id>`, e.g. `lead.won:1042`. `request_id` is
different on every attempt and is for correlating logs, **not** for
deduplication.

**Answer 2xx when you have it** — including for a replay you have
already processed. A 200 to a duplicate tells the CRM the event is
delivered, which is correct: the CRM does not need to know whether
you meant "done" or "done already".

### What the CRM does with your answer

| You answer | The CRM does |
|---|---|
| `2xx` | marks it delivered, never sends it again |
| `429` | retries, honouring `Retry-After` |
| `5xx` | retries |
| `408`, `409`, `425` | retries |
| any other `4xx` | **stops.** Marks it dead and waits for a person |
| connection refused, timeout | retries |

Retries are at 1, 5, 15, 60, 120, 360, 720 and 720 minutes — eight
attempts over roughly twelve hours, enough to ride out a deployment
or an overnight outage.

A `400` is treated as permanent on purpose: it means you will not
accept that payload, and sending it another seven times will not
change that. It becomes visible to the CRM administrator rather than
disappearing into a retry loop.

### If the CRM is not configured

With `TMS_WEBHOOK_URL` unset, events are still queued. They are not
lost; they are delivered once somebody configures the destination.

## 6. The client block register

Procam management can block a client. When they have, the CRM refuses
to create an account or a lead for that client **to the TMS as firmly
as to its own screens**:

```
403 {"ok": false, "error": {"code": "client_blocked",
                            "message": "This client is blocked by
                                        management. Reason: …"}}
```

Show the message to the user. Do not retry it and do not work around
it — the attempt is recorded against the register either way.

### Ask before you start

```
POST /api/integration/v1/client-restriction/check
```

Bearer-authenticated like everything else here, and **read-only** —
it never creates or changes a restriction.

Send whatever identifiers you hold. They are tried in this order and
the first match wins, so prefer the stable ones:

```json
{"crm_account_id": 42,
 "gstin": "27AAAAA0000A1Z5",
 "pan": "AAAAA0000A",
 "email": "buyer@customer.test",
 "tms_project_id": "TMS-PRJ-7001",
 "company_name": "Some Customer Ltd",
 "business_unit": "PLPL"}
```

| field | note |
|---|---|
| `crm_account_id` | the CRM's integer id. **Prefer this.** |
| `gstin`, `pan` | exact, unambiguous |
| `email` | an exact contact address |
| `tms_project_id` | resolved through `crm_tms_links` to the CRM lead and its account |
| `company_name` | last resort. Matched on a normalised name; a near miss answers `caution`, never `blocked` |
| `business_unit` | `PLPL` or `PWLPL`, when a restriction covers only one |

The answer:

```json
{"ok": true,
 "blocked": false,
 "level": "none",
 "reason_code": null,
 "reason": null,
 "message": null,
 "restriction_id": null,
 "company_name": null,
 "matched_on": null,
 "resolved_crm_account_id": 42,
 "resolved_crm_lead_id": null,
 "effective_from": null,
 "effective_until": null,
 "review_date": null,
 "scope": null,
 "business_units": null,
 "request_id": "…"}
```

- **`blocked`** is the field to branch on. `level` is
  `none` | `caution` | `blocked`; a `caution` is not blocked — show
  the `message` and let the user continue.
- **`effective_until` is always `null`.** The register has no expiry:
  a block runs until somebody lifts it. `review_date` is a reminder
  to look again, not an end date, and is reported as itself. The
  field is present so you do not have to special-case its absence.
- **A `tms_project_id` the CRM has never seen** answers `200` with
  `blocked: false` and a `note`, not an error. Most of your projects
  will not be linked, and that is not a failure.

| error code | status |
|---|---|
| `unauthorized` | 401 |
| `integration_disabled` | 503 |
| `missing_identifier` | 400 — you sent no identifiers at all |
| `malformed_request` | 400 — not a JSON object, or a non-numeric `crm_account_id` |

The old `GET /CRM/api/client-restriction/check` still exists and
still requires a browser session. It was left alone deliberately:
weakening it so one more caller could reach it would have been the
wrong repair.

One thing worth knowing: **a check against a restricted client is
recorded** on the register's timeline as an attempt, attributed to
`tms:<your token name>`. "Who keeps trying to raise work for a
blocked client" is a question administrators ask, and the answer
should not depend on which system it was tried from. Nothing about
the restriction itself changes.

---

## 7. Identifiers

Use `crm_lead_id` and `crm_account_id`. They are integers and they do
not change.

**Never key on a subject line or a company name.** Both get edited,
and a cross-reference that breaks when somebody fixes a typo is not a
cross-reference. The CRM stores the pairing in `crm_tms_links`; the
TMS should store its own copy of `crm_lead_id` on its project.

---

## 8. What the TMS still has to build

Nothing in this list exists yet, and none of it can be written from
inside the CRM repository.

1. **A settings page or config** for the CRM base URL and the bearer
   token. The token is a secret: environment or secret store, not
   source.
2. **A client-restriction check** before Client PO, Job and LR
   creation — blocked refuses, caution warns. §6.
3. **Calls to `/accounts` and `/leads`** when the TMS needs a CRM
   record that may not exist, with idempotency keys.
4. **A `crm_lead_id` column** on the TMS project, and the
   `/link-tms` call when a project is created from CRM work.
5. **A webhook endpoint** for §5, idempotent on `Idempotency-Key`,
   answering 2xx on acceptance.
6. **Its own integration log**, so a failure is visible from the TMS
   side too. Debugging across two systems where only one keeps
   records means reading the other team's logs.

---

## 9. Trying it

```bash
TOKEN=...   # from the CRM administrator
BASE=https://procamlogitech.com/CRM/api/integration/v1

curl -s -H "Authorization: Bearer $TOKEN" "$BASE/ping"

curl -s -H "Authorization: Bearer $TOKEN" \
     "$BASE/accounts?name=Some%20Customer%20Ltd"

curl -s -X POST -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' \
     -H 'Idempotency-Key: tms-demo-0001' \
     -d '{"company":"Some Customer Ltd","project":"Trial"}' \
     "$BASE/leads"
```

Run the last one twice. The second answers `"replayed": true` and
creates nothing — that is the behaviour to build against.

---

## 10. There is no reconciliation endpoint, and why

An earlier draft of this contract suggested the TMS should poll as
well as listen. That was wrong — you cannot discover a `lead.won` you
never received by polling, because the polled state of a won deal is
identical whether or not you were told. Adding a
`GET /events?since=…` endpoint would have been building a second
mechanism to paper over a first one that did not work.

So the first one was fixed instead. Delivery is durable: the event is
written down before anything is sent, retried for about twelve hours
across eight attempts, and visible to a CRM administrator if it
cannot be delivered at all. **Retries are the recovery mechanism.**

If an event does go dead — eight failures, or a `4xx` from you — the
CRM administrator can requeue it after the cause is fixed, and it
arrives with the same `event_id` it always had. Nothing needs you to
go looking.

If it later turns out you do need to reconcile — say the TMS database
is restored from a backup and loses events it had already accepted —
say so and the endpoint is a small addition on top of
`webhook_outbox`, which already holds every event with an immutable
id and a creation time. It is not built now because nothing currently
needs it, and an unused endpoint is one more thing to keep honest.

---

## 11. Configuration

| CRM variable | What it is |
|---|---|
| `CRM_INTEGRATION_TOKENS` | `name:token` pairs, comma separated. One pair for the TMS. Unset means every integration route answers 503 |
| `TMS_WEBHOOK_URL` | where the CRM posts `lead.won` and `quote.won` |
| `TMS_WEBHOOK_TOKEN` | the bearer token the CRM sends with them |

There is one token system, not two: the same `CRM_INTEGRATION_TOKENS`
that authenticates the TMS calling in. The TMS's own token for
inbound CRM webhooks is `TMS_WEBHOOK_TOKEN` and is the TMS's to
choose.
