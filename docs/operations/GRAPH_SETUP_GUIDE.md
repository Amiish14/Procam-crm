# Microsoft Graph Setup Guide

**For:** the Microsoft 365 / Entra administrator at Procam.
**Goal:** let the CRM read and send mail for **one mailbox only**,
`leads@procamgroup.in`, and nothing else in the tenant.

Nothing in this guide is run by the CRM team. The CRM side (server
commands in §6 and §7) runs after IT confirms §1–§5.

## What the CRM needs, and why

| Permission (type: **Application**) | Used for | Without it |
|---|---|---|
| `Mail.Read` | Real-time lead ingest from the leads mailbox; reading back the 17 damaged enquiries | New enquiries are not ingested in real time; recovery fails with HTTP 403 |
| `Mail.Send` | "A lead was assigned to you" notification emails, sent **from** leads@procamgroup.in | Each notification fails with 403 (logged; nothing else breaks) |

Not needed: `Mail.ReadWrite` (the CRM never moves, deletes or flags
mail; the legacy "mark as read" option is off), any delegated permission,
any access to other mailboxes.

Optional: `User.Read.All` lets the webhook resolve the mailbox's directory
id for its mailbox check. Without it the check falls back to the address
and still works. Leave it out unless a notification is wrongly rejected.

| | |
|---|---|
| App registration (client) ID | `bd542cf9-f851-4bcb-b731-70e3e0b2ae42` |
| Mailbox | `leads@procamgroup.in` |
| Webhook | `https://procamlogitech.com/CRM/api/email/webhook` |

Application permissions apply to **every mailbox in the tenant** until an
Application Access Policy restricts them. So the order below matters:
**restrict first, grant second.**

---

## 1. Check the current state

Microsoft Graph PowerShell:

```powershell
Install-Module Microsoft.Graph -Scope CurrentUser        # once
Connect-MgGraph -Scopes "Application.Read.All","AppRoleAssignment.ReadWrite.All"

$appId = "bd542cf9-f851-4bcb-b731-70e3e0b2ae42"
$sp    = Get-MgServicePrincipal -Filter "appId eq '$appId'"
$graph = Get-MgServicePrincipal -Filter "appId eq '00000003-0000-0000-c000-000000000000'"

# Application permissions currently granted to the CRM app
Get-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $sp.Id |
  ForEach-Object { $id = $_.AppRoleId; ($graph.AppRoles | Where-Object Id -eq $id).Value }

# Client secret expiry
(Get-MgApplication -Filter "appId eq '$appId'").PasswordCredentials |
  Select-Object DisplayName, EndDateTime
```

Note any secret expiring within 60 days (§5).

## 2. Restrict the app to the leads mailbox — BEFORE granting

Exchange Online PowerShell:

```powershell
Install-Module ExchangeOnlineManagement -Scope CurrentUser   # once
Connect-ExchangeOnline -UserPrincipalName <admin>@procamgroup.in

New-ApplicationAccessPolicy `
  -AppId bd542cf9-f851-4bcb-b731-70e3e0b2ae42 `
  -PolicyScopeGroupId leads@procamgroup.in `
  -AccessRight RestrictAccess `
  -Description "Procam CRM: leads@procamgroup.in only"

Get-ApplicationAccessPolicy | Where-Object AppId -eq "bd542cf9-f851-4bcb-b731-70e3e0b2ae42"
```

If your tenant prefers a group (so a second mailbox can be added later
without a new policy), create a mail-enabled security group containing
only the leads mailbox and use its address as `-PolicyScopeGroupId`:

```powershell
New-DistributionGroup -Name "Procam CRM Graph Scope" -Alias procam-crm-graph-scope `
  -Type Security -Members leads@procamgroup.in
```

Microsoft notes a policy can take up to about an hour to take effect.

> Microsoft is moving Exchange app access to **RBAC for Applications**
> (`New-ManagementScope` + `New-ManagementRoleAssignment` for a service
> principal). If your tenant already uses it, grant the roles
> `Application Mail.Read` and `Application Mail.Send` scoped to the leads
> mailbox instead of §2–§3, and tell the CRM team — §4's tests are the
> same.

## 3. Grant the application permissions (admin consent)

Either in the portal — Entra admin center → App registrations →
the CRM app → API permissions → Add → Microsoft Graph → **Application
permissions** → `Mail.Read`, `Mail.Send` → **Grant admin consent** — or
with PowerShell:

```powershell
Connect-MgGraph -Scopes "AppRoleAssignment.ReadWrite.All","Application.Read.All"
$appId = "bd542cf9-f851-4bcb-b731-70e3e0b2ae42"
$sp    = Get-MgServicePrincipal -Filter "appId eq '$appId'"
$graph = Get-MgServicePrincipal -Filter "appId eq '00000003-0000-0000-c000-000000000000'"

foreach ($perm in "Mail.Read","Mail.Send") {
  $role = $graph.AppRoles | Where-Object {
    $_.Value -eq $perm -and $_.AllowedMemberTypes -contains "Application" }
  New-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $sp.Id `
    -PrincipalId $sp.Id -ResourceId $graph.Id -AppRoleId $role.Id
}
```

(For cross-checking: the Application role ids are `Mail.Read`
`810c84a8-4a9e-49e6-bf7d-12d183f40d01` and `Mail.Send`
`b633e1c5-b582-4048-a93e-9f11b44c7e96`. The script above looks them up by
name, so it does not depend on these.)

A **Delegated** `Mail.Read` or `Mail.Send` does not work — the CRM signs
in as itself, with no user.

## 4. Prove the restriction (IT)

```powershell
# must be Granted
Test-ApplicationAccessPolicy -Identity leads@procamgroup.in `
  -AppId bd542cf9-f851-4bcb-b731-70e3e0b2ae42

# must be Denied — pick any other real mailbox
Test-ApplicationAccessPolicy -Identity <someone>@procamgroup.in `
  -AppId bd542cf9-f851-4bcb-b731-70e3e0b2ae42
```

Send the CRM team both results. **Do not proceed if the second is
Granted.**

## 5. Client secret hygiene

- If the secret expires within 60 days: Certificates & secrets → New
  client secret (24 months) → give the value to the CRM administrator
  through the password manager → they update `MS_CLIENT_SECRET` in the
  server's `.env` and restart → then delete the old secret.
- Set a calendar reminder 30 days before the new expiry.

---

## 6. CRM-side Graph tests (CRM administrator, on the server)

Run after IT confirms §4. Each is read-only unless marked.

```bash
cd /var/www/procam-crm

# a) token and roles: must list Mail.Read and Mail.Send, and probe the mailbox with 200
.venv/bin/python scripts/2026_09_02_check_graph_permissions.py

# b) reading mail: dry run over the last 2 hours, writes nothing
.venv/bin/python scripts/2026_09_02_poll_leads_mailbox.py --hours 2 --dry-run

# c) real-time subscription: renews or creates it (writes the subscription id file)
.venv/bin/python scripts/subscribe_leads_mailbox.py --enforce

# d) sending: one test email to yourself (SENDS MAIL)
.venv/bin/python scripts/2026_09_02_test_notification.py --to <your address>
```

| Test | Pass |
|---|---|
| a | `roles` contains `Mail.Read` and `Mail.Send`; message probe status 200 |
| b | Lists recent messages (or "nothing new"); no 403 |
| c | Prints a subscription id and expiry about 2.9 days out |
| d | Exit 0; the email arrives from leads@procamgroup.in |

Then install the subscription renewal timer
(`docs/operations/deploy/procam-crm-graph-subscription.*`) — without it
the subscription lapses in under three days and real-time ingest stops
silently.

## 7. Recovering the damaged enquiries

Seventeen leads had their original enquiry overwritten by the old notes
box before that was fixed. Each still carries its email's message id, so
the email can be read back from the mailbox. This needs §3's `Mail.Read`.

```bash
cd /var/www/procam-crm

# 1. where things stand (read-only)
.venv/bin/python scripts/email_recovery_report.py --list

# 2. backup
.venv/bin/python scripts/backup_database.py --label pre-recovery

# 3. look every message up; writes nothing
.venv/bin/python scripts/2026_09_22_recover_lost_emails.py --check

# 4. recover (WRITES) — saves every replaced value first
.venv/bin/python scripts/2026_09_22_recover_lost_emails.py --all

# 5. the result
.venv/bin/python scripts/email_recovery_report.py --list
```

Step 5 prints the recovery report:

| Line | Meaning |
|---|---|
| restored from the mailbox | Recovered |
| of which still damaged | Still unrecoverable — the message was not found (deleted from the mailbox, or older than `--lookback-days`, default 400) |
| of which text reads as email | Flagged by the migration but holding a real email; not damage, not touched |
| have no message id | Can never be looked up; the text is only in the flagged note |
| Damaged text kept as a flagged note | Preserved as notes — the overwritten text was saved as a note and is never removed |
| Recovery: N of M damaged enquiries restored (P%) | Recovery percentage |

What the recovery guarantees:

- Only leads flagged as damaged are touched. `--ids` can narrow that set
  but never add a healthy lead to it.
- The trail row replaced is the one for that message (or the damaged row
  without a message id) — never a later customer reply.
- Every replaced value is written to
  `backups/recovery_preimage_<timestamp>.json` before commit, and
  `--rollback <that file>` restores it (Rollback Guide §C).

For any lead still unrecoverable after step 4, try a longer window once:

```bash
.venv/bin/python scripts/2026_09_22_recover_lost_emails.py --check --lookback-days 900
```

If the message is simply not in the mailbox any more, the lead stays as
it is, with its preserved note. That is the final state for that lead.
