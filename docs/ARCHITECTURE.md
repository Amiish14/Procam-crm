# Architecture

For developers and the Infrastructure Team: how the CRM is built, where
each responsibility lives, and the rules that hold the parts together.

## Runtime

```
Browser ──https──> nginx (/CRM/) ──> gunicorn (2+ sync workers, 127.0.0.1:8002)
                                        │
                                        └─ Flask app (app.py + app/ + presales/)
                                              │
                           ┌──────────────────┼────────────────────┐
                        SQLite            Microsoft Graph        Optional AI
             /var/www/procam-crm/        (leads mailbox:          (private model /
               procam_crm.db             webhook + sendMail)       embedder; external
                                                                   providers for intake
                                                                   and outreach)
```

- One Flask application, served by gunicorn under systemd
  (`procam-crm.service`) behind nginx at the `/CRM` prefix
  (`URL_PREFIX`, `ProxyFix`).
- One SQLite database. Writes serialise; reads are concurrent. Every
  worker opens its own connections.
- Scheduled work runs as systemd timers calling scripts in `scripts/`
  (backups, Graph subscription renewal, Copilot index, SLA sweep, data
  quality snapshots, ops status). Templates: `docs/operations/deploy/`.
- The app has no background worker process. Long work is a script on a
  timer, never a thread inside a request.

## Code layout

| Path | Responsibility |
|---|---|
| `app.py` | Application object, core models (Lead, Company, Contact, Opportunity, Employee, notes, emails, classifications), authentication, the lead/contact/company/opportunity APIs, boot (`init_db`: create_all, column autoheal, indexes), blueprint registration |
| `app/access/` | **Access control.** `service.py` — the Access Matrix (permissions, data scope, `require`); `scope.py` — record visibility per entity; `records.py` — RFQ, quote, handover and account rules |
| `app/services/` | Shared business services: lead assignment, contact definition, audit trail (`audit.py`, `audit_listener.py`, `config_audit.py`), intake classifier (`lead_intake*.py`), card OCR, vertical recommendation |
| `app/intake/` | Lead review queue, intelligence dashboard, learning from corrections, vendor master |
| `app/copilot/` | Procam AI: intent catalogue (`intents.py`, `queries.py`), retrieval (`retrieval.py`), private-model adapter (`model.py`), vocabulary, question library, gated write actions |
| `app/rfq/`, `app/quote/`, `app/handover/` | Deal documents: RFQs and rate sourcing, quotes and approval, won-deal handover and PO capture |
| `app/company360/`, `app/pic360/` | Account and person 360 views |
| `app/reports_v2/`, `app/funnel/`, `app/triage/`, `app/my_work/` | Reporting, funnels, lead triage, personal pendency |
| `app/data_quality/` | Data-quality checks, dashboard, batch correction, trends |
| `app/bulk_admin/` | Bulk archive / assign / delete and the audit trail screen |
| `app/master_data/`, `app/excel_io/` | Vocabularies; Excel templates and import |
| `app/ops/` | Operational health checks and the admin status page |
| `app/training/`, `app/help/`, `app/notifications/` | Academy, help content, notifications |
| `app/utils/` | Upload handling and validation, formula-safe exports |
| `presales/` | Account development and projects (pre-sales) |
| `email_ingest/` | Graph client, webhook, single-message pipeline, parser, extractors, notifier |
| `templates/`, `static/` | Server-rendered pages; `templates/app.html` is the main single-page application |
| `scripts/` | Migrations (`YYYY_MM_DD_*.py`), operational tools, reports |
| `tests/` | pytest suite (one shared SQLite database per run) |

## Request lifecycle

1. **ProxyFix** restores scheme, host and prefix from nginx.
2. **Session guard** (`_session_guard`, `before_request`): signed-out →
   nothing to check; deactivated account → session cleared; session
   version older than the account's → session cleared (password reset,
   sign-out-everywhere); temporary password still in force → `/api`
   refused; role and vertical refreshed from the database.
3. **CSRF** (Flask-WTF) on every state-changing request; the header is
   `X-CSRFToken`, read by pages from the `csrf_token` cookie. The Graph
   webhook and CSP report endpoints are the only exemptions.
4. **Rate limits** (Flask-Limiter): sign-in per IP; Copilot questions and
   notes search per signed-in person.
5. **View**: checks access with the Access Matrix
   (`require('<perm>')`, `require_permission`) and narrows every query
   with `app/access/scope.py` / `records.py`.
6. **ORM flush**: the audit listener writes an `audit_events` row for
   every watched field change in the same transaction.
7. **Errors**: any `/api/` error, including 401/403/404/413/429/500,
   answers JSON (`{ok: false, error, code}`); pages keep HTML.
8. **Headers**: enforced CSP, HSTS, frame denial, Permissions-Policy,
   COOP/CORP, `no-store` on API responses.

## Cross-cutting rules

| Rule | Where it is enforced |
|---|---|
| The Access Matrix is the only authority on who sees and does what | `app/access/*`; test `test_every_route_has_a_gate.py` fails on an unguarded route |
| Every important change is audited | `app/services/audit_listener.py` (automatic), `audit.record` for non-ORM actions |
| Original customer emails are never overwritten | notes are a separate store; the recovery script touches only flagged damage and saves a preimage |
| Schema changes are additive | boot autoheal + indexes; migrations with `--check` / `--down`; preflight drift check |
| CRM data never reaches a public AI API from the Copilot | `app/copilot/model.py` refusal; preflight private-host check |
| No data is modified automatically by data-quality tooling | preview → confirm → audited batch correction only |
| Secrets live only in `.env` | `.env.example` has none; the audit and preflight never print values |

## Email intake

```
Graph webhook ──> email_ingest/webhook.py (clientState + tenant + mailbox checks)
                    └─> single_message.py ──> classifier (app/services/lead_intake.py,
                          rules first, optional AI step for the uncertain band)
                          ├─ new lead          → Lead + LeadEmail + assignment
                          ├─ reply/quote/etc.  → filed on the existing lead's trail
                          ├─ needs review      → /lead-review queue
                          └─ non-business      → recorded, not a lead
                        every decision → email_classifications (training data)
```

Details: [Classification Guide](operations/CLASSIFICATION_GUIDE.md),
[Lead Ingestion Policy](LEAD_INGESTION_POLICY.md).

## Procam AI Copilot

Deterministic intent catalogue first; an optional private model only
chooses an intent and narrates; retrieval is permission-filtered before
ranking; write actions are off by default and always propose → confirm.
Details: [AI Configuration Guide](operations/AI_CONFIGURATION_GUIDE.md)
and [Copilot Guide](operations/COPILOT_GUIDE.md).

## Reference

- [API and pages](reference/API.md) — generated
- [Database schema](reference/SCHEMA.md) — generated
- [RBAC](RBAC.md) · [Security](SECURITY.md) · [Database](DATABASE.md)
- [Developer Guide](DEVELOPER_GUIDE.md) · [User Guide](USER_GUIDE.md)
- [Operations](operations/README.md)
