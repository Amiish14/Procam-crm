# R3 — quote revisions, conditions and the PDF (`FEATURE_QUOTE_REV_V2`)

CRM-QUOTE-REV-02. Off by default.

## Revision numbering (D2)

A revision keeps the first quote's number: `QUO-2026-0045 /
Rev-00`, `Rev-01`, `Rev-02`. Today each revision takes a brand new
`QUO-YYYY-NNNN`, so the customer who received 0045 is sent 0061
and has to be told they are the same quotation.

`quote_number` is **not** reused or rewritten. It stays unique and
stays the internal identifier; `root_quote_id` and `rev_no` carry
the display. Changing what a stored identifier means is how you
lose the ability to find anything later.

A revision now needs a reason, from a Master Data list, and on
sending, the previous revision becomes Superseded — except one
already Won or Lost, which is history.

## Conditions (§5.2)

Approved **customer-facing** `rfq_conditions` flow into the
quote's special conditions at consolidation. Internal notes never
travel; that is why `visibility` is recorded when the line is
written rather than judged afterwards.

Duplicates are merged on normalised text and **counted**, not
silently dropped.

**Conflicts block approval.** A conflict is the same topic —
validity, payment, free time — carrying different numbers. Two
conditions that merely mention validity are fine; two that name
30 days and 45 days are a dispute waiting for the customer to
find it, and approval stops with both halves quoted.

## PDF (§5.5)

Behind the flag the customer copy gains a Business Unit
letterhead, a customer block, "Our ref RFQ-…, Lead #…", a
confidentiality clause and "For <BU> — Authorised Signatory".

It **loses the internal status line**, which has been telling
customers the quotation is a "Draft".

The letterhead reads `business_unit` from admin settings and
falls back to the company name alone. A letterhead with an
invented address is worse than a plain one.

## Deploy

    cd /var/www/procam-crm && git pull
    .venv/bin/python scripts/2026_10_26_quote_rev.py --check
    .venv/bin/python scripts/2026_10_26_quote_rev.py
    .venv/bin/python scripts/2026_10_26_quote_rev.py --backfill --apply
    sudo systemctl restart procam-crm

The backfill walks `parent_quote_id` and will report **0**:
production holds no quotes. It exists because the first real
revision needs a root, and a backfill nobody has run is one
nobody can trust later.

Settings: `business_unit` = `{"name": …, "address": …, "gstin": …,
"signatory": …}`. Without it the PDF prints the company name and
no address.

## Not in R3

§5.3's cost-sheet panel and §5.4's quote-mail matching and
unmatched queue are **not built**. Both need decisions I would
otherwise be guessing at:

- the cost sheet reads "the lowest received vendor reply", which
  exists only in the freight rate tables — extending it to all
  verticals is R5's vendor work, not a quote change;
- §5.4 says to reuse the FF-COMMS-01 unmatched queue, and Stage A
  found there is no queue to reuse: `ff_inbound.link` returns a
  reason to its caller and nothing persists it. Building the
  queue is real work, and keying it on quote-number-plus-revision
  depends on the numbering above being live first.

Until those land, step 8 has no event and a chain stops at 7.
