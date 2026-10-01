# Granting Mail.Send to the Procam CRM

**For:** the Microsoft 365 / Entra administrator at Procam. You do not
need to know anything about the CRM to do this.
**Time:** about ten minutes, plus propagation.
**Risk:** none to any other mailbox, provided §5 is done.

---

## 1. The ask, in one line

Grant the **application** permission `Mail.Send` on Microsoft Graph to
this existing app registration, give admin consent, and confirm the
Exchange application access policy that limits it to one mailbox.

| | |
|---|---|
| Application (client) ID | `bd542cf9-f851-4bcb-b731-70e3e0b2ae42` |
| Display name | the Procam CRM app registration (the one that already reads the leads mailbox) |
| Permission to add | Microsoft Graph → **Application** → `Mail.Send` |
| Already granted | `Mail.Read` |
| The only mailbox it may touch | `leads@procamgroup.in` |

The app signs in as itself, with no user, so **Delegated** permissions do
nothing for it. It must be the Application variety, and application
permissions only take effect after an administrator consents.

## 2. Why, and what is broken until then

The CRM is the system Procam's sales team works out of. It sends its
outbound mail through Graph, from the leads mailbox, with
`POST /users/leads@procamgroup.in/sendMail`. The app registration has
`Mail.Read` — so incoming enquiries are ingested — but not `Mail.Send`,
so **every outbound message fails** with HTTP 403 `ErrorAccessDenied`:

* **assignment emails** — "a lead has been assigned to you". A salesperson
  is given an enquiry and is never told.
* **Workbench reminders** — overdue follow-ups, quotes going stale,
  deadlines due today.
* **the five scheduled reports** — the daily action list, the end-of-day
  exceptions, the weekly report to each user, the weekly report to each
  vertical head, and the monthly report to management.

Nothing crashes: the CRM logs the 403 and carries on. That is precisely
the problem — the failure is silent to the people waiting for the mail.

`Mail.Send` does not grant the ability to read, move or delete anything.
It is not `Mail.ReadWrite`, which the CRM does not want.

## 3. Grant it in the portal

**Entra admin center** (entra.microsoft.com) →
**Applications** → **App registrations** →
*(All applications, search for* `bd542cf9-f851-4bcb-b731-70e3e0b2ae42` *)* →
**API permissions** →
**Add a permission** →
**Microsoft Graph** →
**Application permissions** →
search `Mail.Send` → tick **Mail.Send** →
**Add permissions** →
**Grant admin consent for Procam** → **Yes**.

Done correctly, the API permissions table then shows, for Microsoft
Graph:

| Permission | Type | Status |
|---|---|---|
| `Mail.Read` | Application | Granted for Procam |
| `Mail.Send` | Application | Granted for Procam |

If the **Grant admin consent** button is greyed out you are not a
Privileged Role Administrator or Global Administrator in this tenant;
someone who is must click it. The permission is inert until they do.

## 4. The same thing in PowerShell

```powershell
Install-Module Microsoft.Graph -Scope CurrentUser        # once
Connect-MgGraph -Scopes "AppRoleAssignment.ReadWrite.All","Application.Read.All"

$appId = "bd542cf9-f851-4bcb-b731-70e3e0b2ae42"
$sp    = Get-MgServicePrincipal -Filter "appId eq '$appId'"
$graph = Get-MgServicePrincipal -Filter "appId eq '00000003-0000-0000-c000-000000000000'"

$role = $graph.AppRoles | Where-Object {
  $_.Value -eq "Mail.Send" -and $_.AllowedMemberTypes -contains "Application" }

New-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $sp.Id `
  -PrincipalId $sp.Id -ResourceId $graph.Id -AppRoleId $role.Id

# read it back: both Mail.Read and Mail.Send should be listed
Get-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $sp.Id |
  ForEach-Object { $id = $_.AppRoleId; ($graph.AppRoles | Where-Object Id -eq $id).Value }
```

Creating the app role assignment this way **is** the consent — there is
no second step. (For cross-checking: the Graph application role id for
`Mail.Send` is `b633e1c5-b582-4048-a93e-9f11b44c7e96`. The script looks
it up by name, so it does not depend on that value.)

## 5. Restrict the app to the one mailbox — this matters

A Graph **application** permission is tenant-wide by default. Granted on
its own, `Mail.Send` would let this app send mail as *any* mailbox in
Procam — the managing director's included — and the recipient would see
nothing unusual. That is not what anyone is agreeing to, and it is not
what the CRM needs.

An Exchange Online **application access policy** fences the app into the
mailboxes you name, inside Exchange, regardless of what Graph would
otherwise allow:

```powershell
Install-Module ExchangeOnlineManagement -Scope CurrentUser   # once
Connect-ExchangeOnline -UserPrincipalName <admin>@procamgroup.in

New-ApplicationAccessPolicy `
  -AppId bd542cf9-f851-4bcb-b731-70e3e0b2ae42 `
  -PolicyScopeGroupId leads@procamgroup.in `
  -AccessRight RestrictAccess `
  -Description "Procam CRM: leads@procamgroup.in only"
```

Then prove it both ways — one mailbox allowed, every other denied:

```powershell
# must say Granted
Test-ApplicationAccessPolicy -Identity leads@procamgroup.in `
  -AppId bd542cf9-f851-4bcb-b731-70e3e0b2ae42

# must say Denied — pick any other real mailbox
Test-ApplicationAccessPolicy -Identity <someone>@procamgroup.in `
  -AppId bd542cf9-f851-4bcb-b731-70e3e0b2ae42
```

If the second result is **Granted**, stop and fix the policy before
telling the CRM team the grant is done.

If the policy already exists from the earlier `Mail.Read` work
(`Get-ApplicationAccessPolicy` lists it), you do not need a new one: the
policy is per application, not per permission, so it covers `Mail.Send`
the moment it is granted. Confirm it is there and re-run the two tests.

> If your tenant has moved to **RBAC for Applications**
> (`New-ManagementScope` + `New-ManagementRoleAssignment` for a service
> principal), grant the role `Application Mail.Send` scoped to the leads
> mailbox instead of this section. The verification in §7 is unchanged.

## 6. How long it takes to work

| Step | Typical | Allow up to |
|---|---|---|
| Admin consent visible in Entra | immediately | — |
| Consent in a newly issued token | 1–5 minutes | ~15 minutes |
| A token the CRM already holds | — | **1 hour** |
| A new or changed application access policy | 5–30 minutes | ~1 hour (Microsoft's own guidance) |

Tokens are cached for about an hour and carry the permissions they were
issued with, so the CRM can keep getting 403 for up to an hour after a
correct grant. That is expected and needs no intervention: the next
token picks the permission up. Verify after fifteen minutes, and only
investigate if it still fails after an hour.

## 7. Verify (the CRM administrator runs this)

On the CRM server, in the application directory:

```bash
.venv/bin/python scripts/check_mail_send.py
```

It asks Microsoft for a token and reads the permissions recorded inside
it. It sends no mail, so it is safe to run before, during and after the
change. Expected output once the grant has propagated:

```
--- application permissions on this token (2) ---
  Mail.Read
  Mail.Send

CAN SEND — Mail.Send is granted. ...
```

Then prove delivery end to end with one real message:

```bash
.venv/bin/python scripts/check_mail_send.py --send-test you@procamgroup.in
```

The script refuses to attempt a send while `Mail.Send` is absent, so a
failure here is always a real failure, never a missing permission.

The same fact also appears in the routine monitoring, with no extra
step:

```bash
.venv/bin/python scripts/ops_status.py --only graph_mail_send
.venv/bin/python scripts/production_preflight.py   # CONFIG section
```

and on the admin page at **/CRM/admin/ops**, where it is `OK` once
granted and `WARN` while missing.

## 8. If it is granted and mail still fails

In this order:

1. **Wait out the token cache** — up to an hour (§6).
2. `scripts/check_mail_send.py` lists `Mail.Send` but a send returns 403:
   the permission is fine and Exchange is refusing. Re-run the two
   `Test-ApplicationAccessPolicy` commands in §5 — the leads mailbox must
   be **Granted**. A policy that names a group the mailbox has been
   removed from denies everything.
3. **403 for the mailbox only** — confirm `leads@procamgroup.in` still
   exists, is licensed or is a shared mailbox in this tenant, and is not
   blocked for sending.
4. **401, not 403** — that is the client secret, not this permission.
   See `GRAPH_SETUP_GUIDE.md` §5.

## See also

* `GRAPH_SETUP_GUIDE.md` — the whole Graph setup, of which this is one
  step, including the client secret and the webhook subscription.
* `NOTIFICATIONS.md` — what the CRM sends, to whom, and when.
