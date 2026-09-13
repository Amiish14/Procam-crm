# Security

For the Security Team, developers and CRM Administrators: the controls
in place, where each lives, how it is tested, and what remains outside
the software.

## Controls

### Identity and sessions

| Control | Detail | Code / test |
|---|---|---|
| Passwords | Werkzeug salted hashes; minimum 10 characters; may not contain the employee code, be a common password (digits/symbols stripped), or reuse the current one | `app.py password_problem` · `test_session_and_signin.py` |
| Temporary passwords | Random 12 characters on create/reset, shown once, expire after 72 hours, must be changed before any API works | `test_account_security.py` |
| Lockout | 8 failed sign-ins lock the account for 15 minutes, counted in the database (holds across workers); plus 5/minute, 20/hour per IP | `test_session_and_signin.py` |
| Session cookie | Secure, HttpOnly, SameSite=Lax, 8-hour lifetime, path always set | `app.py` config · `test_the_session_cookie_always_has_a_real_path` |
| Session revocation | Session version bumps on admin reset, deactivation, own password change (current session kept) and "sign out everywhere" | `_session_guard` |
| Live role | Role and vertical re-read on every request; deactivated accounts cleared immediately | `_session_guard` |
| Super admin protection | Only the super admin may change the super admin account | `_refuse_super_target` |
| Guessable-password audit | `scripts/audit_default_passwords.py` (employee code or published passwords; prints codes only) | `test_audit_default_passwords.py` |

### Authorisation

The Access Matrix is the only authority; every record query is scoped;
out-of-scope ids answer 404. Full detail: [RBAC](RBAC.md). A test fails
if any route lacks an access check (`test_every_route_has_a_gate.py`).
Leak tests cover leads, opportunities, accounts (full/partial/routing),
contacts, RFQs, quotes, handovers, business cards, imports, outreach,
notes, Copilot answers and search.

### Request protection

| Control | Detail |
|---|---|
| CSRF | Flask-WTF on every state change; `X-CSRFToken` header; exemptions: Graph webhook (clientState-verified) and CSP reports (no side effects) |
| Content Security Policy | Enforced: scripts only from self and cdnjs, no plugins, `base-uri`/`form-action` self, no framing; `unsafe-inline` retained (inline handlers). `CSP_MODE=report-only` to back out; a test keeps the policy in step with the origins templates use |
| Other headers | HSTS (2 years, subdomains), X-Frame-Options DENY, nosniff, Referrer-Policy, Permissions-Policy (no camera/mic/location/payment/USB), COOP and CORP same-origin, `Cache-Control: no-store` on `/api` |
| Rate limits | Sign-in; Copilot 40/min & 600/hour per person; notes search 60/min; CSP reports 30/min; webhook 120/min. `RATELIMIT_STORAGE_URI` shares counters across workers |
| JSON errors | Every `/api` error is JSON; no stack traces or server paths in responses |
| Size | 20 MB request limit (`MAX_CONTENT_LENGTH`) |

### Data handling

| Control | Detail |
|---|---|
| Upload validation | Content must match the extension (magic bytes); ZIP/Office files inspected without extraction for entry count, expanded size, compression ratio, traversal, macros; text files without NUL bytes — `app/utils/file_validation.py` |
| Downloads | Attachments served as `application/octet-stream` attachments with nosniff and a sandbox CSP |
| Stored XSS | Server templates autoescape (no `safe` filter or `Markup`). Pages that build HTML in the browser escape interpolated values through `e()` / `esc()` / `escHtml()`; the sinks found in the 2026 audit (card scanner, lead state, team names) are fixed and new UI follows the same rule. `unsafe-inline` in the CSP means escaping, not the policy, is the defence |
| Formula injection | Every CSV/XLSX export writes cells through `app/utils/spreadsheet_safe.py` |
| SQL injection | ORM or bound parameters throughout; identifiers in DDL come from constants |
| Secrets | Only in `.env` (chmod 600); never logged, returned or audited (redacted by name); preflight fails published placeholders |
| PII in the repository | None: employee seed and directory data live in git-ignored `data/private/` |

### AI

| Control | Detail |
|---|---|
| Copilot | Model never writes queries; answers only through scoped catalogue intents; public AI hosts refused; retrieval filtered by permission before ranking; write actions off by default, propose → confirm with signed, expiring tokens |
| Prompt injection (Copilot) | Structural: record text cannot change which query runs or whose data it reads |
| Prompt injection (intake) | Email fenced as untrusted data; an email that tries to steer the model cannot skip the review queue |
| External providers | Groq / Anthropic receive email text, lead details and card images when their keys are set — a documented business decision (AI Configuration Guide) |

### Integrations

| Control | Detail |
|---|---|
| Graph webhook | clientState compared in constant time; fails closed without a secret; tenant id must match; notifications only for the sanctioned mailbox |
| Graph permissions | Application permissions restricted to the leads mailbox by ApplicationAccessPolicy (Graph Setup Guide) |
| Email recovery | Touches only flagged damage; preimage saved before writing; rollback |

### Audit

Every change to leads, opportunities, accounts, contacts, employees,
access profiles, master data, vendor domains, notes, classifier
corrections and deal status is written to `audit_events` in the same
transaction (time, user, role, action, record, old/new values, reason,
IP, user agent). Sign-ins, lockouts, logouts, bulk operations, imports,
configuration changes and audit exports are recorded explicitly.
Deletions also keep a full snapshot in `deletion_audit`. Readable under
Administration → Audit Trail with `admin.access`.

## Reporting a vulnerability

Report to the Development Team through the internal ticketing system,
marked confidential. Do not test against production; use a copy of a
backup on a scratch host.

## Outside the software

| Item | Owner |
|---|---|
| Rotate any credential that ever appeared in git history (the PCM001 bootstrap password, a director account's password in an old script) and decide on a history rewrite | Security Team / Business Owner |
| TLS certificate, nginx configuration, VM hardening, OS patching | Infrastructure Team |
| Off-VM encrypted backups | Infrastructure Team |
| Graph ApplicationAccessPolicy and permission grants | IT |
| Shared rate-limit store (Redis) if strict cross-worker limits are required | Infrastructure Team |
| Retention periods for audit and email evidence | Business Owner |
| Whether external AI providers may process CRM data | Business Owner |
