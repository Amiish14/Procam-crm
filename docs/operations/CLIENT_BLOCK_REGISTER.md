# The client block and caution register

Where management records a client the company will not deal with, or
must be careful with, and where the CRM enforces it.

Two states, and the difference is the whole design:

- **Blocked** — nobody creates or progresses anything for this client.
  Every role, every vertical, every entry channel, the Excel import,
  the email intake and the API. The server refuses it.
- **Caution** — business is allowed. Everyone sees the reason and has
  to acknowledge it before going ahead, and the acknowledgement is
  recorded. This is how somebody new learns about a client before
  dealing with them rather than afterwards.

---

## 1. Deploying it

```bash
cd /var/www/procam-crm
git pull
.venv/bin/python scripts/2026_10_12_client_restrictions.py --check
.venv/bin/python scripts/2026_10_12_client_restrictions.py
.venv/bin/python scripts/seed_help_notifications.py
sudo systemctl restart procam-crm
```

Then the daily sweep — review reminders and the attempts digest:

```bash
cd /var/www/procam-crm/docs/operations/deploy
sudo cp procam-crm-restriction-sweep.service \
        procam-crm-restriction-sweep.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now procam-crm-restriction-sweep.timer
```

There is no feature flag. A register with nothing in it restricts
nothing, so the safe state is the empty one and the switch is the
first entry.

### Who may approve

`admin.restrictions`, a new permission in the Access Matrix. By
default the super admin and administrators hold it; a vertical head or
a director is given it in **Admin → User Access Matrix**, which is
what makes the approver list configuration rather than code.

**Anyone may recommend.** The person who notices that a client is
trouble is rarely an administrator.

---

## 2. The first entry

The migration will seed Walchandnagar Industries Limited, with its
aliases, but **only when the reason is given on the command line**:

```bash
.venv/bin/python scripts/2026_10_12_client_restrictions.py \
  --seed-wil --approver <APPROVER-CODE> \
  --category "Payment default / outstanding" \
  --reason "<what actually happened, in a sentence>"
```

Without `--category` and `--reason` it seeds the categories and skips
the client, and says so. A block is a management decision with a
stated reason; the reason is what every refused salesperson reads, and
a migration that invented one would be putting words in somebody's
mouth.

The same entry can be made on the screen instead, which is usually
better: the form warns about the things the command line cannot.

---

## 3. How a client is matched

In this order. The first answer wins.

1. **Account id, GSTIN, PAN, or an exact email address.** No scoring.
2. **The email domain** — but only for an entry whose scope is *the
   whole group*. A dispute with one company must not silently block
   its sister company on the same domain.
3. **The name**, lower-cased, punctuation removed, and the suffixes
   people type six ways — Ltd, Limited, Limit, Pvt, Private, Co,
   Company, Inc, LLP, Corp — stripped. So "Walchandnagar Industries
   Limited", "Walchandnagar Industries Ltd" and "Walchandnagar
   Industries Limit" are one name, and aliases are matched the same
   way.
4. **Similarity**, for what is left. At **0.90 and above** it is the
   client. Between **0.80 and 0.90** it is *possibly* the client,
   which is always a caution even when the entry is a block — being
   wrong in that band should cost a dialog, not a deal.

A name only enters the similarity test if its first significant word
is recognisably the same. Without that, "Nagar Industries" scores 0.80
against "Walchandnagar Industries" purely because one contains the
other, and an unrelated company is warned about for ever.

> **One deliberate difference from the brief.** §4 says 0.90 and above
> is a match; acceptance test 9 calls a single-letter typo
> ("Walchandnagr Industries", which scores 0.98) a *caution*. Those
> cannot both hold. This follows §4 and blocks it: a block somebody
> can step around by mistyping one letter is not a block. To switch to
> the other reading, return `'caution'` instead of `_level_for(row)`
> for `matched_on == 'name-fuzzy'` in
> `app/services/client_restrictions.py`.

---

## 4. Where it is enforced

Server-side, in every path. Hiding a button is not enforcement.

| Path | Blocked | Caution |
|---|---|---|
| New lead | 403 | 409, then allowed once acknowledged |
| Lead company change, or a move to RFQ Generated / Quoted / Won | 403 | acknowledge |
| Contact, Account | 403 | acknowledge |
| RFQ create, and every status advance | 403 | acknowledge |
| Quote submitted for approval | 403 | acknowledge; approval already routes to the vertical head |
| Opportunity | 403 | acknowledge |
| Handover | 403 | acknowledge |
| Business card scan | 403 | acknowledge |
| Excel / bulk import | that row refused, the rest imported, refused rows named back | imported |
| Email intake | **no lead is created** — logged as `blocked_client` | lead created, badge shown |
| API, Procam AI | the same 403, because it is the same route | the same 409 |
| TMS | `GET /api/client-restriction/check` | same |

The caution protocol is worth knowing: the first request gets **409**
with the reason, not 403. The work is not forbidden, it is
*unfinished*. The browser shows the warning, the person ticks the box,
and the same request is sent again carrying `restriction_ack: <id>`.
An acknowledgement names the entry it is for, so a stale tick cannot
wave a different warning through.

---

## 5. What happens to work already in flight

On approving a block, the administrator is shown what would close
before anything happens. On confirming:

- open leads → **Not Interested**, reason "Client blocked by
  management", follow-up cleared
- RFQs → **Withdrawn**
- open quotes → **Lost**
- the PIC of every closed record is emailed, and everyone who sells is
  told the client is blocked

They leave My Work, the follow-up lists, the digests and the
escalation timers by virtue of being closed — the board reads only
open stages. There is no separate "frozen" flag to go stale.

**Read-only** means narrower than it sounds, deliberately. A closed
record refuses the edits that would restart work on it — the stage,
the follow-up date, the next action, the values. Everything else is
still editable, because an administrator has to be able to fix a wrong
owner or a typo on a record that is now part of the history. Reopening
one is the hole that would make the whole register advisory, so that
is the part that is shut.

**Won and in-execution work is never auto-closed.** A job on the road
has cargo, a vendor and an obligation. It is listed for management
instead.

---

## 6. What is never done

- **Nothing is deleted.** Lifting a block keeps the whole history,
  because the next person to ask "have we had trouble with these
  people?" needs the answer after the block has gone.
- **Lifting needs a reason**, and the reason is kept.
- **The money is not shown to everyone.** Everybody may read that a
  client is blocked and why. Dispute amounts, references and attached
  documents need `admin.restrictions` or `admin.access`.

---

## 7. The TMS

```
GET /CRM/api/client-restriction/check?company_name=...&email=...&gstin=...
    &business_unit=PLPL
→ {"level": "blocked"|"caution"|"none", "restriction_id": 1,
   "company_name": "...", "reason_summary": "...", "message": "..."}
```

It needs a session, like everything else. An endpoint that confirms
which customers are in dispute is not one to leave open, and the TMS
runs on the same VM.

The TMS should refuse Client PO, Job and LR creation on `blocked`, and
warn on `caution`. That half is not built here — it is a change in the
TMS, and this is the endpoint it calls.

---

## 8. Checking it works

```bash
# nothing is restricted yet
.venv/bin/python -c "
from app import app
from app.services import client_restrictions as r
with app.app_context(): print(r.check(company_name='Some Client').to_dict())"

# the daily sweep, without sending
.venv/bin/python scripts/restriction_sweep.py --dry-run
```

On the screen: `/CRM/admin/restrictions`. Attempts against the
register are on each entry's own page, and the daily digest reports
the pattern — one person trying once is a mistake, the same person
three times is a conversation.
