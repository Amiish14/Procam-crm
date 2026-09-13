# Role-based access control

For developers, the Security Team and CRM Administrators. How the CRM
decides who may open a screen, call an API, and see a record.

## The one rule

**The Access Matrix decides.** A role name ("admin", "user") only seeds
a sensible default for someone the matrix has not configured. Every
check in the code asks the matrix — directly or through the scope
helpers — and a test fails if a route has no access check at all.

## Two dimensions

| | Question | Stored in | Read with |
|---|---|---|---|
| **Permissions** | *May this person use this function?* | `access_profiles.perms` | `app.access.service.can(perm)`, `@require('<perm>')`, `require_permission` |
| **Data scope** | *Whose records may they see?* | `access_profiles.data_scope` (`all` / `vertical` / `own`) | `app.access.scope.current()` and the entity helpers |

Plus two fixed facts that no matrix edit can change:

- **Super admin** (`employees.is_super_admin`): holds every permission
  and company-wide scope; the only person who may edit the matrix,
  permanently delete, or change the super admin account.
- **Active account and session version**: a deactivated account or a
  reset password ends sessions on the next request.

### Defaults (no stored profile)

| Employee | Scope | Permissions |
|---|---|---|
| super admin | all | all |
| role `admin` | all | all |
| vertical head | vertical | reports + team baseline |
| everyone else | own | team baseline (RFQs, quotes, handovers, funnels, competitors, business cards) |

A stored profile replaces the default entirely. Administrators always
keep `admin.access` so the matrix cannot lock itself.

## Permissions catalogue

Source: `PERMISSION_GROUPS` in `app/access/service.py`.

| Permission | Grants |
|---|---|
| `reports.action` · `reports.competitor` · `reports.accounts` | the report families |
| `module.rfq` · `module.quotes` · `module.handovers` · `module.funnels` · `module.competitors` · `module.business_cards` | the modules |
| `module.handovers_all` | the whole handover queue, not only handovers of the person's deals |
| `admin.access` | the Access Matrix screen; the audit trail and ops status |
| `admin.employees` | create, edit, deactivate employees; reset passwords; full employee list |
| `admin.email` | mailbox subscriptions and the ingestion inbox |
| `admin.master` | master data, vendor master, help content, KPI targets, market intelligence, account/agent deletion, imports of others, batch data correction |
| `admin.triage` | lead triage, bulk assignment, reassigning owners, creating records for someone else, opportunity transfer |

## Record visibility

Every list and every by-id route runs the same rule, so a link can never
open what the list hides. Out-of-scope ids answer **404**, not 403, so
ids cannot be probed.

| Record | Visible when | Code |
|---|---|---|
| Lead | primary or secondary owner within scope | `scope.leads` |
| Opportunity | owner within scope (or its lead visible, for deep links) | `scope.opportunities` |
| Account (Company) | full: primary/secondary owner within scope, or team member · partial: the viewer has leads/opportunities on it (sees those and contacts) · routing: owner and header only | `scope.companies`, `records.company_access` |
| Contact | owner within scope, or at an account in scope, or at an account with the viewer's leads | `scope.contacts` |
| Note / email / activity | its lead is visible | `scope.notes`, `scope.emails`, `scope.activities` |
| RFQ | driver/creator in scope; its lead/opportunity/account visible or team member; a rate line they source or support; `Rate_Sourcing` role | `records.rfqs` |
| Quote | preparer/creator/submitter/approver in scope; its RFQ visible; its lead/opportunity/account visible or team member | `records.quotes` |
| Handover | PIC/creator in scope; its quote/RFQ/opportunity/account visible; Operations or Finance department; `module.handovers_all` | `records.handovers` |
| Copilot passage | lead owner or secondary within scope (filter applied before ranking) | `copilot/retrieval.py` |
| Business card | uploader, or company-wide scope | `business_card/routes.py` |
| Audit events | `admin.access` | `bulk_admin/routes.py` |

Scope codes: `all` → no filter (`codes is None`); `vertical` → the
viewer, everyone in the same vertical, and anyone reporting to the viewer
(`vertical_head_id`); `own` → the viewer.

## Write rules that differ from read rules

| Action | Rule |
|---|---|
| Edit a note | its author, or scope reaching the author |
| Edit/delete a contact | scope reaching its owner; reassign needs company-wide scope |
| Edit an account | company-wide scope, account owners in scope, or its creator |
| Edit an opportunity | scope reaching its owner; transfer needs `admin.triage` |
| Approve a quote | company-wide scope or `Vertical_Head` role; not the quote's own preparer (unless company-wide scope) |
| Submit a rate | the line's sourcing owner, `Rate_Sourcing` role, or company-wide scope |
| Reassign a lead | `admin.triage`, with a reason |
| Permanent deletion | super admin, with a reason, audited before deletion |

## Adding an endpoint

1. Gate it: `@require('<existing permission>')` for a function, or the
   module's `_require_auth` wrapper (which already maps to a module
   permission).
2. Narrow every query with the scope helper for its entity. For a by-id
   route, fetch through the helper and answer 404 when it is absent.
3. Write the leak test: an Own-scope user must not reach another
   owner's record, by list or by id; and the allowances (owner, team,
   scope) must still work.
4. Run `scripts/generate_reference_docs.py`; the gate should appear in
   `docs/reference/API.md`, and `tests/test_every_route_has_a_gate.py`
   must pass.

Never check `session['role'] == 'admin'`.
