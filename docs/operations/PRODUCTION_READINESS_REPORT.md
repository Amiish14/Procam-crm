# Procam CRM — Production Readiness Report

**Date:** 13 September 2026 · **Code:** `main` (the commits after the
final-audit release `2aacd46`) · **Tests:** 1,343 passed, 1 skipped in
file order; in reverse order, the same 3 failures that predate this
work.

## Verdict

**Conditionally ready.** Nothing the software controls blocks go-live. Six
go-live items (deploy, passwords, secrets, backups, subscription renewal,
worker count) are configuration and account actions on the server, each
with a command and a check. Microsoft Graph permissions are the one
external dependency. They block email recovery and notification emails,
but not day-to-day use.

**Readiness score: 72 / 100** (method in §11). The score rises to about
81 once the six go-live items are closed. It stays below 90 until Graph
permissions are granted and the pending business decisions are made.

---

## 1. What this pass found and fixed

Every finding was verified in the code before it was changed. Each fix
has behavioural tests, and each guard was mutation-checked: removing it
fails a test.

### Security

| # | Finding | Severity | Fix |
|---|---|---|---|
| S1 | Any admin could reset the **super admin's** password, demote it, or deactivate it | Critical | Only the super admin may change that account |
| S2 | Every new or reset password **was the employee code**; 115/115 accounts on the local copy still open with it, 5 of them admins | Critical | Random one-time passwords; audit script for production |
| S3 | A default-password session could use every `/api` route | High | API refuses until the password is changed |
| S4 | A **deactivated** employee's session kept working for up to 8 h | High | Checked on every request |
| S5 | DEPLOY.md published the PCM001 bootstrap password | High | Removed; audit script tests it on every account; **git history still has it** |
| S6 | Opportunity by id, convert-to-project, outreach, import commit, business cards, Copilot feedback: no ownership check | High | Scoped like their lists (15 leak tests) |
| S7 | Copilot search kept a lead's text findable by its **previous owner** after bulk reassignment or deletion | High | Index entries dropped on every such path |
| S8 | Copilot action tokens signed with a **public constant** when `SECRET_KEY` was unset | High | Signed with the app key |
| S9 | Graph webhook accepted **any POST** when its secret was unset | High | Fails closed |
| S10 | Stored XSS on the business-card scanner (names from inbound email senders) | High | Escaped |
| S11 | Mailbox recovery could overwrite a **healthy** original email (`--ids`, first inbound row) | High | Damaged set only; exact row; preimage saved; `--rollback` |
| S12 | Server path in a 404 message; internal model host shown to all users; unlimited unauthenticated CSP endpoint | Medium | Removed / admin-only / rate-limited |
| S13 | Change-password page printed the default password on screen | Medium | Removed |

### Defects

| # | Finding | Fix |
|---|---|---|
| D1 | An **agent's quotation** to Procam was filed as sent by Procam, moved the lead to Quoted, and put the agent's price in `quoted_amount_inr` | Only our own quotations are outbound. Leads already affected are listed by the `quotes_received_filed_as_sent` report |
| D2 | Copilot search looked only at the **oldest 4,000** passages; search within a lead could find nothing | Filters by lead and words before the cap |
| D3 | **Notes were never indexed** for Copilot search (wrong column name) | Fixed; rebuild the index after deploy |
| D4 | Copilot actions only confirmed within the same second | Uses the token's expiry |
| D5 | Copilot "not helpful" feedback silently rejected (400), which skewed analytics | Free text accepted |
| D6 | Copilot chips linked to three pages that don't exist | Correct pages |
| D7 | Review screen: every item "Classified **(blank)**" | Label sent |
| D8 | Pipeline drawer drew reassignments as blank rows | Shows from → to and reason |
| D9 | Recovery report checked the wrong credential names, and its percentage overstated the damage | Fixed; now matches the recovery script |
| D10 | Intelligence proposals with an apostrophe had dead buttons | Fixed |

### Operations tooling added

`production_preflight.py`, `/healthz`, `backup_database.py`,
`data_quality_report.py` (13 CSVs), `audit_default_passwords.py`,
`perf_benchmark.py`, recovery `--rollback`, systemd timer and logrotate
templates, a corrected `.env.example`, and the nine operations guides.

---

## 2. Feature validation (UI → API → database)

✅ connected and tested · 🟡 works, with a stated limitation

| Feature | Status | Evidence / limitation |
|---|---|---|
| Lead email history | ✅ | Trail panel → `GET /api/leads/<id>/emails` → `lead_emails`; tests |
| Original email preservation | ✅ | Lead save cannot write the enquiry; notes are separate rows |
| Notes revision history | 🟡 | Kept on every edit, API returns it; **no screen shows revisions yet** |
| Draft email trail | ✅ | Draft recorded, "sent" changes status (no copy) |
| Email recovery report | ✅ | CLI by design; recovered / still damaged / preserved / % |
| Lead intake classifier | ✅ | Every decision recorded; kill switch `LEAD_INTAKE_MODE` |
| Duplicate detection | 🟡 | Thread matching is exact; the fuzzy duplicate score computes 2 of its 7 signals (max 75 against a 70 threshold) |
| Vendor classification | 🟡 | Exact-domain match; no screen to list or remove supplier domains |
| Quote detection | ✅ | Fixed D1 |
| Confidence scoring | 🟡 | Score and parts stored; parts not displayed |
| Assignment learning | ✅ | Proposals from corrections and repeated reassignments |
| Vertical confidence | 🟡 | Stored and in the API; not shown on screen |
| Reassignment history | ✅ | Reason required; history row on every path including bulk |
| Review screen | ✅ | Fixed D7 |
| Classification dashboard | ✅ | FP / FN / rescued figures |
| AI Copilot | ✅ | 37 intents; fixed D2–D6 |
| RBAC | 🟡 | Leads, dashboards, reports, Copilot scoped; S6 fixed; **RFQ / quote / handover by id and Company 360 await a visibility decision** (§5) |
| Prompt-injection protection | 🟡 | Copilot: structural (no model-written queries). Intake AI: email text not marked untrusted |
| RAG | ✅ | Permission-filtered; fixed D2, D3, S7; nightly re-index needed |
| Contact service | 🟡 | Shared definition used by the My Work untouched count and the Copilot (a test holds them equal); dashboard "no activity" and My Work "days idle" still use `updated_at` (changing them changes dashboard numbers — decision) |
| API JSON handling | ✅ | Every `/api` error is JSON, including 401/403/404/413/500 |
| Audit trail | 🟡 | Deletions, reassignments, stages, notes, Copilot, classifier, employee changes (journal). **Not audited:** Access Matrix edits, logins, opportunity stage/owner, contact deletion |
| Copilot analytics | ✅ | Feedback now recorded (D5) |
| Academy | ✅ | CSRF fix; JSON errors |

## 3. Deployment verification

| Item | Status | How verified |
|---|---|---|
| Migrations | ✅ | Additive only; each has `--check` and `--down`; this pass adds none |
| Startup auto-heal | ✅ | Boot adds missing columns; test drops columns and reboots; false "FAILED" on absent tables removed |
| Indexes / FKs / constraints / nullability | ✅ tooling | Preflight compares the live schema with the models (columns FAIL, tables and indexes WARN). SQLite does not enforce FKs; the app deletes dependants explicitly (now consistent between single and bulk delete) |
| Backup | ✅ tooling | Online, verified, 0600; a test proves consistency during a write. **Not yet scheduled on the VM** |
| Rollback | ✅ documented | Code-only rollback works on the newer schema; restore procedure; recovery undo |
| Restart | ✅ | `/healthz`; process start → healthy in 1.1 s on the 10,000-lead scratch database |
| Health checks | ✅ | `/healthz` (200 / 503), preflight, runbook |
| Repeatable deploy | ✅ | Every step idempotent (Deployment Guide) |
| **On the VM itself** | ⏳ | Needs the deploy and the verification commands below |

## 4. Performance

Measured locally (12-core laptop) with production's gunicorn settings,
on seeded production-scale data: 10,000 leads, 2,000 accounts, 20,500
emails, a 2,000-lead account, a 500-email thread, a 15 MB attachment.
**Re-run on the VM** against a backup copy; the VM probably has fewer
cores, so expect lower throughput.

### Single requests (admin, all data)

| Endpoint | Cold | Warm p50 | Warm p95 |
|---|---|---|---|
| Lead list (500 rows, 1.4 MB) | 110 ms | 97 ms | 118 ms |
| Dashboard summary | 317 ms | 345 ms | 389 ms |
| My Work | 160 ms | 143 ms | 384 ms |
| Copilot "my open leads" | 344 ms | 243 ms | 299 ms |
| Copilot RAG search | 198 ms | 161 ms | 287 ms |
| Company 360 (2,000 leads) | 159 ms | 108 ms | 197 ms |
| Email thread (500 emails, 750 KB) | 16 ms | 13 ms | 15 ms |
| 15 MB attachment download | 31 ms | | |
| 21 MB upload (over limit) | 8 ms → 413 | | |

A sales rep with Own scope sees the dashboard summary in 22 ms and
Copilot "open leads" in 19 ms: cost scales with the data in scope.

### Concurrent users (mixed dashboard / lead list / My Work / Copilot)

| Server | 100 users | 500 users |
|---|---|---|
| **2 sync workers (production today)** | 10 req/s, p50 9.2 s, 0 failed | 10 req/s, 1,094 connect timeouts |
| 5 sync workers | 20 req/s, p50 4.8 s, 0 failed | 20 req/s, 1,061 connect timeouts |
| 5 sync workers + SQLite WAL | 22 req/s, p50 4.3 s, 0 failed | 22 req/s, 1,042 connect timeouts |
| 5 workers × 4 threads | 3.3 req/s, p50 8.6 s | 6 req/s, 156 read timeouts |

### Bottlenecks

1. **Worker count.** Throughput scales with worker processes, not threads.
   Threads were 3–6× worse (Python's GIL plus SQLite locking). With 2
   workers the CRM serves about 10 requests a second, and a page load
   makes several.
2. **Dashboard summary** loads every lead as a full object (≈10,000 per
   request) and totals them in Python.
3. **Copilot "open leads"** loads every open lead in scope as a full
   object.
4. **Lead list** sends about 2.8 KB per lead (1.4 MB for 500).
5. 500 simultaneous connections exceed the listen queue. On macOS part of
   that is the OS backlog limit, so re-measure on the VM.

**Recommendation (configuration, measured):** set gunicorn workers to
`2 × vCPU + 1` sync workers after confirming the VM's cores
(`nproc`) and memory (each worker ≈ 150–250 MB). Re-run the benchmark on
a backup copy before and after. WAL mode is a further +10%, but changes
how the database files look on disk (`-wal`, `-shm`); adopt it only with
the backup and restore procedures updated. Items 2–4 are code
optimisations for a later release.

## 5. Pending business decisions

| # | Decision | Why it matters | Default until decided |
|---|---|---|---|
| B1 | **Public AI providers.** Email text goes to Groq/Anthropic; lead details and card photos to Anthropic | Contradicts the "no CRM data to public AI" rule the Copilot follows | Unchanged; preflight WARN |
| B2 | Who may open an **RFQ / quote / handover** by id (beyond driver/preparer: pricing, approvers, ops) | They are readable by id by any signed-in user today | Unchanged (tables nearly empty) |
| B3 | **Company 360** shows contacts and deal values to everyone ("existence is not secret" was the stated policy) | Wider than lead visibility | Unchanged |
| B4 | Retention periods for `copilot_log`, `email_events`, classifier email bodies, `deletion_audit` | Growth, privacy | Kept indefinitely; nothing deleted |
| B5 | Dashboard "no activity" and My Work "days idle": adopt the shared contact definition | Changes numbers people track | Unchanged |
| B6 | May any admin make another admin, or only the super admin? | Privilege escalation between admins | Any admin (unchanged) |
| B7 | The 226 SuperProcure leads | Data | Untouched |
| B8 | Sea Freight / Air Freight service mapping | Copilot glossary | Unmapped |
| B9 | Enable Copilot actions (`PROCAM_AI_ACTIONS`) | Writes by assistant | Off |
| B10 | Leads an agent's quote moved to Quoted (report D1) | Pipeline value accuracy | Listed, not corrected |

## 6. External dependencies

| Dependency | Owner | Blocks | Guide |
|---|---|---|---|
| Graph **Mail.Read** (Application) + admin consent | Procam IT | Recovery of 17 enquiries; real-time ingest if not already granted | Graph Setup §3 |
| Graph **Mail.Send** (Application) + admin consent | Procam IT | Assignment notification emails | Graph Setup §3 |
| **ApplicationAccessPolicy** restricting the app to leads@procamgroup.in — **before** granting | Procam IT | Safe grant | Graph Setup §2, §4 |
| Graph client secret expiry | Procam IT | Ingest stops at expiry | Graph Setup §5 |
| nginx `/CRM/` block (lives in the TMS repo) | Infra | Rebuild on a new VM | DR Guide |
| Self-hosted model / embedder | Infra (optional) | Free-form Copilot phrasing, semantic search | AI Config Guide |
| Off-VM backup storage (Azure Blob / Azure Backup) | Infra | Surviving VM loss | Backup Guide |

## 7. Configuration remaining (on the VM)

| Item | Check / action |
|---|---|
| `SECRET_KEY` set, 32+ chars | preflight PASS |
| `EMAIL_WEBHOOK_SECRET` set, not the example | preflight PASS |
| `.env` is `chmod 600` | preflight PASS |
| `URL_PREFIX=/CRM`, secure cookies, DEBUG off | preflight PASS |
| Daily backup timer | `deploy/procam-crm-backup.*` |
| Graph subscription renewal timer (or confirm the existing one) | `deploy/procam-crm-graph-subscription.*` |
| Nightly Copilot index rebuild | `deploy/procam-crm-copilot-index.*` |
| SLA sweep timer (or confirm existing) | `deploy/procam-crm-sla-sweep.*` |
| Log rotation for gunicorn files | `deploy/logrotate-procam-crm` |
| journald size cap | `/etc/systemd/journald.conf` `SystemMaxUse=1G` |
| gunicorn workers | §4 recommendation, after `nproc` |
| `NOTIFY_ENABLED` | leave on (403s are logged) or `false` until Mail.Send |
| `PROCAM_AI_*` | optional; private hosts only |

## 8. Optional enhancements (not required for go-live)

- Screens for note revision history, vertical confidence and score breakdown.
- The five uncomputed duplicate-score signals; a supplier-domain admin screen.
- Dashboard summary and Copilot "open leads" as SQL aggregates; a slimmer lead-list payload.
- A generic audit table covering Access Matrix edits, logins and opportunity changes.
- Server-side session revocation (log out everywhere; demotion takes effect immediately).
- Redis-backed rate limiting shared across workers; per-account login throttling; Copilot request limits.
- An enforced nonce-based CSP (today report-only with `unsafe-inline`).
- Untrusted-content markers in the intake AI prompt.
- A CSV formula-injection guard on exports.
- Copilot model host: an allow-list inside the app, matching the preflight's private-host rule.
- A per-module test database, to remove the test-order dependence below.

## 9. Known limitations

- Copilot search is refreshed nightly (once the timer is installed), not live.
- RFQ / quote / handover detail routes and Company 360 are not
  owner-scoped (B2, B3).
- About 25 legacy routes check the session role instead of the Access
  Matrix. Role is read at login, so a demotion takes effect at the next
  login (deactivation is immediate).
- Logout clears the browser cookie; a copied cookie stays valid until it
  expires (8 h).
- Rate limits are per gunicorn worker (in-memory).
- SQLite on one VM: a single point of failure, and write concurrency is
  limited (§4).
- Three tests fail when the suite runs in reverse file order. They also
  failed before this work began, and the cause is shared test state, not
  product code.
- Performance figures are from a laptop, not the VM.

## 10. Risk register

| ID | Risk | Likelihood | Impact | Mitigation | Owner | Status |
|---|---|---|---|---|---|---|
| R1 | Account takeover through default or published passwords | High until audited | Critical | `audit_default_passwords.py --list`; reset all listed, admins first; rotate PCM001 | CRM admin | **Open — go-live** |
| R2 | Graph subscription lapses; enquiries stop arriving silently | High without a timer | High | Renewal timer; weekly preflight WARN on age | CRM admin | **Open — go-live** |
| R3 | Data loss (no scheduled or off-VM backups) | Medium | Critical | Backup timer + weekly off-VM copy + monthly restore test | CRM admin / Infra | **Open — go-live** |
| R4 | Slow or queued pages at peak with 2 workers | Medium | Medium | Worker change after measuring on VM | CRM admin | Open |
| R5 | CRM data processed by public AI providers | Certain while keys set | Medium–High (policy) | Decision B1 | Business owner | Open — decision |
| R6 | Records readable by id outside the owner (RFQ/quote/handover, 360) | Low (tables near-empty) | Medium | Decisions B2, B3 | Business owner | Open — decision |
| R7 | Graph granted tenant-wide without the access policy | Low if guide followed | Critical | Policy first, `Test-ApplicationAccessPolicy` Denied check | IT | Open — external |
| R8 | Deploy breaks a page | Low | High | Preflight, backup, code-only rollback | Deployer | Mitigated |
| R9 | Pipeline stage/value wrong on leads hit by D1 | Certain for some leads | Medium | Report, manual review | Sales ops | Open — data |
| R10 | Unowned leads (≈5,000) never worked | Certain | Medium | Data-quality reports, owner mapping | Sales ops | Open — data |
| R11 | Client secret expiry stops ingest | Medium over 24 months | High | Expiry check monthly; reminder | IT | Open |
| R12 | Journal/logs fill the disk | Low | Medium | logrotate, journald cap, preflight disk check | CRM admin | Open — config |
| R13 | Copilot model pointed at a public API | Low | High | App deny-list + preflight private-host FAIL | CRM admin | Mitigated |
| R14 | Recovery overwrites good data | Low | High | Damaged-set guard, preimage, `--rollback` | CRM admin | Mitigated |

## 11. Readiness score — method

Each area is scored 0–100 on evidence; weights reflect go-live impact.

| Area | Weight | Score | Basis |
|---|---|---|---|
| Features and correctness | 20 | 90 | §2: all connected; seven 🟡 with stated limits |
| Security | 20 | 72 | Criticals fixed in code; production password state unknown (R1); decisions B2/B3; CSP, sessions, rate limits (§8) |
| Deployment and operations | 15 | 78 | Tooling and guides complete; not yet run on the VM; timers not installed |
| Data protection (backup / DR) | 10 | 55 | Tooling ready; nothing scheduled, no off-VM copy yet |
| Performance | 10 | 65 | Fast single requests; ~10 req/s capacity at current config |
| External dependencies | 10 | 40 | Graph permissions not granted |
| Data quality | 5 | 45 | Known gaps (unowned leads, overdue closes, won without PO) |
| Documentation | 10 | 95 | Nine guides, runbook, this report |
| **Weighted total** | 100 | **72** | 71.85, rounded |

## 12. Checklists

### Deployment checklist (this release)

- [ ] Mac: full suite passes; `git push origin main`; note the hash
- [ ] Server: `production_preflight.py` before; understand any FAIL
- [ ] Server: `backup_database.py --label pre-deploy` → `check ok`
- [ ] Server: `git pull --ff-only`; hash matches
- [ ] Server: the three final-audit migrations `--check` (apply if listed)
- [ ] Server: restart; `/healthz` ok; journal clean; preflight 0 schema FAIL
- [ ] Server: `build_copilot_index.py` (notes now indexed)
- [ ] Browser smoke test (Deployment Guide §7)

### Go-live checklist

- [ ] R1 `audit_default_passwords.py --list` → every listed admin reset; plan for the rest
- [ ] PCM001: rotated if it ever used the published password (or deactivated if unused)
- [ ] Preflight config: `SECRET_KEY`, `EMAIL_WEBHOOK_SECRET`, `.env` 600 all PASS
- [ ] Backup timer installed and one run verified; first off-VM copy taken
- [ ] Graph subscription renewal scheduled (new timer or confirmed existing)
- [ ] Copilot index timer and logrotate installed
- [ ] `nproc`, memory checked; worker count decided and benchmark re-run on a backup copy
- [ ] Data-quality reports generated and owners named for each
- [ ] Graph request sent to IT (Graph Setup Guide)

### Sign-off checklist

- [ ] Engineering: deployment verified on the VM; preflight output attached
- [ ] Security: R1 closed; R7 plan accepted; §8 items scheduled or accepted
- [ ] Operations: runbook owner named; backups and restore test done
- [ ] Business owner: decisions B1–B10 recorded (decide, or accept the default)
- [ ] IT: Graph §2–§4 complete, or dated
- [ ] Sales ops: data-quality owners and deadlines agreed

---

## Appendix — read-only production verification commands

Run on the server after the deploy. None of these change anything.

```bash
cd /var/www/procam-crm
git log --oneline -1
systemctl is-active procam-crm
curl -s http://127.0.0.1:8002/healthz
.venv/bin/python scripts/production_preflight.py
.venv/bin/python scripts/audit_default_passwords.py
.venv/bin/python scripts/email_recovery_report.py
.venv/bin/python scripts/build_copilot_index.py --check
.venv/bin/python scripts/data_quality_report.py
nproc; free -m; df -h /var/www
systemctl list-timers --all --no-pager | grep -i procam
journalctl -u procam-crm --since "1 hour ago" --no-pager | grep -cE "Traceback|autoheal FAILED"
# every /api error is JSON, never HTML
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' -X POST http://127.0.0.1:8002/api/academy/basics/practice
```
