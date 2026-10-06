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
 "request_id": "…",
 "sent_at": "2026-10-06T04:12:00Z",
 "source": "procam-crm",
 "data": { … }}
```

with `Authorization: Bearer <TMS_WEBHOOK_TOKEN>`, `X-Request-Id`, and
an `Idempotency-Key` — **use it**; the CRM may retry.

| event | data |
|---|---|
| `lead.won` | `crm_lead_id`, `crm_account_id`, `company`, `project`, `value`, `assigned_to` |
| `quote.won` | `crm_quote_id`, `crm_lead_id`, `crm_account_id`, `quote_number`, `value`, `currency` |

Answer `2xx` for accepted. Anything else is logged as an error on the
CRM side with the request id, and is visible to the CRM
administrator.

The CRM treats this as best effort: a deal is won in the CRM whether
or not the TMS could be reached. **If the TMS must never miss one, it
should also poll** `GET /leads?…` rather than relying on the webhook
alone.

---

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

The TMS can ask before it starts:

```
GET /CRM/api/client-restriction/check?company_name=…&gstin=…
→ {"level": "none" | "caution" | "blocked", "message": "…"}
```

That endpoint currently needs a signed-in session rather than the
integration token, which is a wart. If the TMS needs it
server-to-server, say so and the CRM will expose it under
`/api/integration/v1/` too.

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
