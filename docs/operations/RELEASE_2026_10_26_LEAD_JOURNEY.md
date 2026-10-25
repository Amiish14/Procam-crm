# R4 — pre-RFQ funnel and the lead record

`FEATURE_PRE_RFQ_FUNNEL` and `FEATURE_LEAD_JOURNEY`, both off.

Two flags because the flowchart is a way of looking and the
activity log changes data; one can go to people a week before the
other.

## The activity moves the stage

641 accounts have no `dev_stage` and 392 are "Active Account"
with almost nothing between — a funnel with no middle, because
recording an activity and moving the stage are two jobs and the
second does not happen. One save now does both.

**Meeting planned and Meeting held are separate kinds.** The
existing `ACTIVITY_KINDS` has one "Meeting", which cannot tell a
diary entry from a thing that happened, and that difference is
the whole point of the stage it moves.

**Stages only move forward.** A call logged after a meeting does
not drag the account back to Contact Established.

A "No" on the capability check closes the lead as Lost / "We
could not serve it", and says so in the response — a dropdown
that quietly closes a lead is one people learn to fear.

## Targets (§6.3, D8)

Per month, vertical and person. RFQs received are counted from
`registered_at`; nothing is typed. `leads_needed` divides the
target by the person's own blended conversion, and returns
**None** when there is no channel mix behind it — dividing by an
empty mix prints infinity, and "we do not know" is the honest
cell.

## Steps 11 and 12

Step 11 closes on a PO or LOI reference, **required**. "Won" with
no reference cannot be checked against the customer's system
later. Step 12 closes on the handover pack that already exists —
not a new handover.

## Lead channel — a decision is needed before the backfill

Four channels the CRM can decide: an auction, a director's
forward, an account that has won before, and mail from outside.
Anything else stays blank and is asked about; a guessed channel
feeds a conversion rate that reads as measurement.

**The order of those four rules is not settled.** §6.1 lists
inbound first; this build evaluates tender first. Over 11,891
leads the two differ on **470**:

| Channel | as built | §6.1 order |
|---|---:|---:|
| Existing Customer / Repeat | 8,485 | 8,269 |
| Inbound Enquiries | 1,830 | 2,300 |
| Tender / E-Procurement Portals | **254** | **0** |
| (left blank) | 1,322 | 1,322 |

Taking §6.1's order literally erases the Tender channel: every
auction arrives by email from outside, so "inbound" claims it
first. That is why this build puts tender first — it is a
property of what the lead *is*, not of how it reached us.

The open question is the other 216: an emailed enquiry from a
customer who has won before. Repeat converts at 60% and inbound
at 50%, so the choice moves real numbers.

**The backfill has not been applied.** Decide the order and it is
one command:

    .venv/bin/python scripts/2026_10_26_lead_journey.py --backfill --apply

## Deploy

    cd /var/www/procam-crm && git pull
    .venv/bin/python scripts/2026_10_26_lead_journey.py --check
    .venv/bin/python scripts/2026_10_26_lead_journey.py
    sudo systemctl restart procam-crm

## Not in R4

§6.4's time-per-step chart and the tabbed record are specified
against the demo; the journey data (`rfq_chain.journey`) and the
8 + 12 flowchart are built and served, the chart is not. §6.5's
cockpit panels are R5's reporting work.
