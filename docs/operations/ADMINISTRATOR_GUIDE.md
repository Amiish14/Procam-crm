# Administrator Guide

For CRM administrators and the super admin. Pages are under
`https://procamlogitech.com/CRM`.

## Roles in one paragraph

The **super admin** (one account, `is_super_admin`) owns access itself:
the Access Matrix, permanent deletion, and the super admin account. Only
the super admin can change the super admin's account — reset its
password, change its role, or deactivate it. **Admins** manage employees,
assignments, intake and data. Everyone else sees what their **Access
Matrix** profile allows: their own records, their vertical's, or all.

## Employees

App → **Employees**.

| Task | How | Notes |
|---|---|---|
| Add | Add Employee | A **temporary password** is shown once in a dialog — copy it and give it to the person privately. They must change it at first login. |
| Reset a password | Reset password | A new temporary password, shown once. The old one stops working immediately. |
| Deactivate | Deactivate | Takes effect on their **next request** — an open session is ended. |
| Change vertical / role | Edit | Changes to access are also made in the Access Matrix |

Passwords: at least 10 characters, and never the employee code.

Every create, update and deactivation is logged with who did it
(`journalctl -u procam-crm | grep employee_change`).

### Accounts on guessable passwords

Accounts created before September 2026 were given their employee code as
password. Check, read-only:

```bash
.venv/bin/python scripts/audit_default_passwords.py --list
```

It lists employee codes (never passwords) whose password is the code or
a password once published in the repository. For each: reset it (the
person gets a temporary password), or deactivate the account if nobody
uses it. Admin accounts first.

## Access Matrix

App → **User Access Matrix** (super admin). Each person has a data scope
(**Own** / **Vertical** / **All**) and permissions (modules, reports,
admin functions). The matrix is the single authority on visibility: the
lead list, dashboards, reports, Copilot answers and search all read it.

After changing someone's scope, they see the change on their next
request. Leak-check a change by logging in as a test user of that
profile, or ask the Copilot "my open leads" as them.

## Lead intake

| Page | Use |
|---|---|
| **/CRM/lead-review** | Emails the classifier was unsure about. Accept as lead, or one click: Rate sourcing, Quote submission, Internal, Duplicate. Each correction is training data. |
| **/CRM/intake-intelligence** | How the classifier is doing: accuracy, false positives (non-leads that became leads), missed enquiries rescued from review, proposals learned from corrections. Apply or dismiss proposals. |
| **/CRM/accounts/owners** | Which employee owns each account. New email leads are assigned from this. |

Reassigning a lead needs a reason. Repeated reassignments of one
account's leads to the same person turn into a proposal to change the
account owner (future leads only).

## Bulk lead administration

**/CRM/admin/leads**. Archive (reversible) is the normal action. Bulk
assign writes assignment history for every lead that changes owner.
Permanent delete is super admin only, needs a reason, is refused where
history would be destroyed, and writes a snapshot to the audit first
(**/CRM/admin/audit**).

## Data quality

Read-only CSV reports for the review meeting:

```bash
.venv/bin/python scripts/data_quality_report.py
# → reports/data_quality/<date>/*.csv
```

| Report | What to do with it |
|---|---|
| accounts_without_owner | Assign an owner in /accounts/owners |
| leads_without_owner | Assign, or archive if dead |
| opportunities_without_owner | Assign |
| opportunities_overdue_close | Owner updates the date or closes it |
| won_without_po / won_without_handover | Sales captures the PO; creates the handover |
| accounts_without_vertical | Set the vertical |
| accounts_without_service | Record the service on a lead, quote or handover |
| duplicate_accounts / duplicate_contacts | Merge (company dedup tool) after checking |
| accounts_missing_gstin | Add GSTIN (Indian accounts only) |
| incomplete_contacts | Add email/phone, link to an account |
| quotes_received_filed_as_sent | Leads an agent's quote wrongly moved to Quoted before Sept 2026: check stage and quoted amount |

The CSVs hold customer contact details: they are owner-only files in a
git-ignored folder. Don't email or attach them anywhere.

## Copilot

- **/CRM/copilot-analytics**: questions asked, answered vs not, feedback,
  index health.
- Copilot actions are off unless `PROCAM_AI_ACTIONS=on` (AI Configuration
  Guide).

## Academy

**/CRM/academy** for staff; **/CRM/admin/training** for progress and
certificates.

## Audit trails

| What | Where |
|---|---|
| Lead deletions (with snapshot) | /CRM/admin/audit (`deletion_audit`) |
| Reassignments (who, from, to, reason) | Lead → History drawer (`lead_assignment_history`) |
| Stage changes | Lead → History drawer (`lead_stage_history`) |
| Note edits (previous text) | `lead_notes.revisions`, `GET /api/leads/<id>/notes/<note>/revisions` |
| Copilot questions and actions | /CRM/copilot-analytics (`copilot_log`) |
| Employee changes | `journalctl -u procam-crm | grep employee_change` |
| Classifier decisions and corrections | /CRM/intake-intelligence (`email_classifications`) |

Not audited today: Access Matrix edits (only the last editor is kept),
logins, opportunity stage/owner changes, contact deletion. See the
Production Readiness Report.
