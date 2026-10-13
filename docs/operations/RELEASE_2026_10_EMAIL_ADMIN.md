# Release — email administration (CRM-EMAIL-ADMIN-01)

12 October 2026. Vertical heads, recipient policy, switches, employee
addresses, and the outbox throttle.

## Why

Three things were wrong, and all three were invisible from every
screen the CRM had.

**A vertical head was copied on almost nothing.** `employees.vertical`
holds four values. Leads carry nine. Nothing mapped between them, so a
lead in most verticals resolved to no vertical head, and the CC that
was meant to reach a head reached nobody. A missing recipient is not an
error, so nothing said so.

**A notification with four recipients was four emails.** Nobody could
see who else knew, so two people chased the same client, and a reply
went to one person instead of the thread.

**Most undeliverable email was never a mail problem.** It was an
address on the employee record that could not work — a domain the CRM
may not write to, a placeholder minted from an employee code so a form
would save, or one mailbox on two people. None of it appears on the
Email Health screen, because a message that was never sent leaves no
row.

## What changed

### Vertical heads — `Admin → Email → Vertical heads`

`vertical_head` says whose each vertical is (head and optional deputy).
`vertical_alias` maps the spellings leads actually carry onto the
verticals the business manages. `resolve_vertical_head(record)` reads
both.

- A vertical that maps to nothing resolves to **Management**, so
  somebody senior is copied rather than nobody, and the unmapped value
  is reported in the administrators' morning email until it is mapped.
- A row that exists and names nobody behaves the same way, because a
  row with no head is the same silence as no row — and the vertical is
  still listed as a gap so somebody fixes it.
- `employees.is_vertical_head` is kept in step with the master on every
  save. Code written before this table — the weekly head report, the
  escalation ladder, the access-matrix default — still reads that flag,
  and two places holding the same fact is how they drift.
- Vertical heads can **read** the master, so they can tell an
  administrator when it names the wrong person for their own vertical.
  Editing needs `admin.email`.

### Recipients — `Admin → Email → Recipients`

Per event: a To list and a CC list, as roles. The built-in matrix is
still the default; a `notification_policy` row overrides only the parts
it sets, and **no rows are seeded** — an absent row means the matrix is
in force, which is what is in force today.

- One email, with both lines filled. A reply reaches the thread, and
  everyone can see who else knows.
- Nobody appears twice, and nobody who is on To is also on CC — a
  person being asked to act is not also told "for your awareness".
- The standing CC (`always_copy`, seeded with DIR12010) and the
  administrators' own copy list are both folded in as **CC**, not as
  more emails.
- Every save bumps a version, including a save that changes nothing,
  and the version is recorded on each email. "Why did that email go
  there" is answerable in March about an email sent in November.

**The trade.** The recipients of one event now share one email, so a
transport failure costs all of them that email rather than one of
them. The outbox retry covers it, and the bell has already rung for
everybody — that part does not depend on the mail server.

**Quiet hours.** One email cannot honour four different sets of quiet
hours. It waits only if *everybody* on it is in theirs, and then only
until the first of them is up. Anyone who is awake gets it now, which
is what they asked for by not setting quiet hours.

### Switches — `Admin → Email → Switches`

Feature flags moved into `app_settings` under `flag.<NAME>`. A change
takes effect within ten seconds on every worker — no file edit, no
`systemctl restart`. `.env` is still read as the fallback, so a server
configured by the previous release keeps its settings; the screen says
which source is answering.

`EMAIL_KILL_SWITCH` stops all outbound CRM email immediately. Nothing
is lost: the worker claims nothing while it is on, so turning it off
releases the queue intact. It is on if **either** the database or the
environment says so — a kill switch a stale row could countermand is
not a kill switch.

### People — `Admin → Email → People`

Who cannot receive CRM mail, and why. Validation on the employee
create and update API refuses an unusable address and warns (without
refusing) about a duplicate — a shared departmental mailbox is
sometimes what a department wants.

HRMS import at `Admin → Email → People → Import`: CSV or XLSX,
previewed before anything is written, matched on the employee code and
never on the name. **Only email addresses are taken, and no employee
is created** — an HRMS export covers the whole company, and creating a
record for every name would add several hundred employees nobody asked
for. Every change is audited against the employee.

### The outbox

- `EMAIL_MAX_PER_MINUTE`, default 20, settable on the Switches screen.
  Counted from what has actually been sent, so the two gunicorn workers
  and the queue worker share one limit rather than having three.
  Office 365 throttles a mailbox at thirty a minute and answers the
  thirty-first with a 4.7.x, which the CRM used to retry — producing
  more throttling.
- Back-off is now **2, 5, 15, 30, 60** minutes, five tries.
- A **5xx fails at once**, with the code and the server's own words on
  the row and in the morning report. A dead address used to be retried
  five times, which turned one wrong address into five identical log
  lines and buried the reason. A **4xx** waits and retries.
- The email log shows the CC line, the SMTP code and the policy
  version.
- The morning brief and the weekly reports are spread across the
  throttle at queue time rather than all queued for the same instant
  and pushed back two minutes at a time.

### The administrators' morning report

Three new sections, each one a problem that is invisible everywhere
else: email that did not arrive and the reason the server gave,
people who cannot receive CRM mail, and verticals with nobody above
the owner. Each section counts in full and lists up to the section
limit, so the fortieth bad address is not silently absent.

## Deploy

Migrate, then restart. Never the reverse — a restart before the
migration leaves a window in which every lead INSERT fails.

```
cd /var/www/procam-crm
git pull
.venv/bin/python scripts/2026_10_15_email_admin.py --check
.venv/bin/python scripts/2026_10_15_email_admin.py
sudo systemctl restart procam-crm
curl -sS -o /dev/null -w '%{http_code}\n' https://procamlogitech.com/CRM/login
```

The `--check` prints every vertical on live leads that maps to nothing.
That list is the work to do on the screen afterwards; the script does
not guess which vertical "Heavy Cargo" belongs to, because that is a
business decision.

Rollback: `scripts/2026_10_15_email_admin.py --down --yes`. The two
`email_outbox` columns are kept — they hold what the mail server said
about mail that has already been sent.

## What a person still has to do

1. **Name a head for every vertical.** The migration seeds whoever
   `employees.is_vertical_head` already names. Anything it reports as
   "no head named" reaches the owner and nobody above them.
2. **Map the unmapped verticals** the `--check` listed.
3. **Fix the addresses** on `Admin → Email → People`.
4. **Check the standing CC** is who it should be. It is seeded with
   DIR12010 and it is a CC on every notification email, so keep it
   short — a mailbox that receives everything reads nothing.
