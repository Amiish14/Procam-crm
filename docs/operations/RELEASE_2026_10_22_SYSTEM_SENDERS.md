# System senders, and the model label

21–22 October 2026.

## Mail from a machine is not an enquiry

Two Microsoft 365 quarantine digests became leads called
"Microsoft". They sat in the pipeline until somebody closed them,
and one of them is why the word "Claude" was in a notes field —
the digest was listing a spammer's subject line.

The inbox is deliberately the filter: every message in `leads@`
becomes a lead, because a missed enquiry costs more than a junk
one. This narrows that to senders that are definitionally not
customers.

### How it works

`app/services/system_senders.py` holds the rule. The check runs in
`email_ingest/single_message.py` **before the parse and before the
AI call**, so a digest no longer costs a model call.

A held message is written to `mail_ingest_log` with outcome
`blocked` and a reason naming the pattern that caught it. **Nothing
is deleted.** An administrator filtering Mail ingest on `blocked`
sees exactly what was held and why.

`force` overrides it: somebody replaying a message by hand has
already decided.

### The list is editable without a release

Master Data list `system_sender`, in Admin → Master Data. The
seeded patterns are used only while that list is empty — once a
person has touched it, theirs is the list, **including a rule they
switched off**. A rule that came back from the seeds would be a
rule nobody can turn off.

Only `*` is special, and only as a run of characters. Anything
cleverer would be a regex language in a box an administrator types
into, and the first mistake silently holds a customer.

| Pattern | Why |
|---|---|
| `quarantine@messaging.microsoft.com` | the digest that started this |
| `*@messaging.microsoft.com` | Microsoft 365 messaging robots |
| `*@protection.outlook.com` | Exchange Online Protection notices |
| `postmaster@*` | delivery reports, non-delivery notices |
| `mailer-daemon@*` | the mail system reporting a failure |
| `bounce@*`, `bounces@*` | bounce handling |
| `googlealerts-noreply@google.com` | Google Alerts digests, 32 in 90 days |
| `noreply@justdial.com` | directory listing marketing |
| `no-reply@peoplemattersonline.com` | HR conference marketing |

### What the report changed, before anything ran

The first draft of this list had `noreply@*`, `no-reply@*`,
`donotreply@*` and `do-not-reply@*`. The 90-day report said they
would hold **247 leads** — and **226 of them were live freight
enquiries**:

- `noreply@alerts.superprocure.com` and `alerts2`, 226 between
  them: *"Load posted by Isgec Heavy Engineering Ltd"*, with the
  route (BHEL Trichy → ISGEC Rattangarh), vehicle type, tonnage
  and loading date.
- `noreply@us.bn.cloud.ariba.com`: *"Jindal Stainless has invited
  you to participate in an event: RFQ_Transporter Required"*. Two
  are open opportunities with owners right now.

A procurement portal sends from `noreply@` **precisely because**
the reply belongs in the portal rather than the mailbox. The shape
of the address says nothing about whether the message is business.

All four patterns were removed. A robot is named individually,
after somebody has read what it sends. `do-not-reply@hlag.com`,
`noreply@drewry.co.uk` and `noreply@fffai.org` are deliberately
**not** on the list — a carrier's own notices, shipping research
somebody reads, and the freight forwarders' federation.

### Off until the report is read

`FEATURE_SENDER_BLOCK`, off. Run
`scripts/system_sender_report.py --check` first: it says how many
of the last 90 days' email leads each pattern would have held, by
sender and by stage, and **flags any held lead that somebody had
actually worked** — owned, valued, or past New. A pattern that
catches a worked lead is too wide, and that number is the one to
read before switching anything on.

The report also lists the most frequent senders that came
*through*, which is where the next robot shows up.

## The model label

The copilot analytics screen showed the raw model id. It now reads
**Procam AI**, with the id after it in small grey — that page
exists to tell an administrator which model is answering, and
hiding it would make the screen useless for its one job.

`app/services/ai_branding.display()` is the single place the
substitution happens, for this and for the draft API.

## Deploy

```bash
cd /var/www/procam-crm && git pull origin main
sudo systemctl restart procam-crm

# read this before turning anything on
PYTHONPATH=/var/www/procam-crm .venv/bin/python \
  scripts/system_sender_report.py --check

# then, if the numbers are right
PYTHONPATH=/var/www/procam-crm .venv/bin/python \
  scripts/system_sender_report.py --seed
```

Seeding writes the patterns into Master Data for editing. It holds
nothing: the rules run only when `FEATURE_SENDER_BLOCK` is turned
on, in Admin → Email → Switches.

Rolling back is the switch. The held messages stay in
`mail_ingest_log` either way.
