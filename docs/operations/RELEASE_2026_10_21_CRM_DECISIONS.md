# Release — CRM decisions of 21 Oct 2026 (part 1)

Management answered 66 open questions about the CRM on 21 Oct 2026.
Most confirmed what the CRM already does. This release builds the ones
that needed code. Part 2 (vendor master and codes, route codes, rate
requests from the CRM) waits for the vendor and route lists, which
Accounts and the Transport Head are checking.

## What changes for users

| # | Decision | What the user sees |
|---|---|---|
| A2 | A vertical head sees every lead of their vertical, and anyone sees a lead they were given a task on | Leads in their vertical owned by someone outside it now appear; so do other verticals' leads where they hold a task. Own-scope users get only the task allowance. Reviews and "for=" views of one person are unchanged. |
| B4 | Tender / GeM leads go to the tender desk | New Source choice "Tender/GeM" on both lead forms. The lead is owned by the tender desk set on **CRM rules**, whoever keyed it in. |
| B6 | What a manual lead must carry depends on the vertical | Heavy Transport / Project Freight need origin and destination; Warehousing / Installation need a site location; all need a vertical and a cargo / scope line. Mail-created and imported leads are not refused. |
| C6 | Block Godrej (bad paymaster) | `scripts/2026_10_21_block_godrej.py` lists every Godrej account; management picks which to block. |
| C7 | No approval step for new accounts; the director reviews them weekly | "New accounts this week" section in the Thursday review pack, for the people on **CRM rules → new_accounts_reviewer**. |
| E3 | Five lost reasons + Other | The Lost dialog offers: Price too high, Lost to competitor, Client dropped project, No Response, Scope not in our service, Other. Old values stay on old records and in the dashboard filter. |
| F1 | CRM mail from the TMS mailbox, tagged | Every CRM email subject starts with `[CRM]` (setting `mail_subject_prefix`). |
| F4/F5 | Daily report 09:00, weekly pack Thursday 15:00 IST | Timers moved from 08:30 and 14:00. |
| G9 | Freight forwarding buy rates and margin visible to named people only | Quote API and the FF margin screens hide cost, margin and margin % from everyone not on `margin_viewers_ff` — including unrestricted admins. Other verticals: preparer, lead owner, head of the vertical, whole-company viewers. The FF margin endpoints now also check the quote is in the caller's scope (they did not). |
| H1/H3/H4 | Margin minimums per service line, head limit ₹25 lakh, director above | Quote lines take a vehicle choice (PTM own / PTM market / Mechanical). Submitting says whether the director is needed. **Enforcement is behind `FEATURE_MARGIN_GATE` (off).** |
| H7/H8 | Default validity by vertical; overseas clients quoted in USD | Applied when the quote is created and the person typed neither. |

## Deploy

1. Verified backup (Deployment Guide).
2. Pull the branch.
3. `scripts/2026_10_21_crm_decisions.py --check`, then without `--check`.
   Four nullable columns; the app also adds them at boot.
4. Copy the two changed timers and reload:
   `procam-crm-daily-report.timer`, `procam-crm-weekly-pack.timer`, then
   `systemctl daemon-reload && systemctl restart procam-crm-daily-report.timer procam-crm-weekly-pack.timer`.
5. Restart the app.
6. **Admin → Email → CRM rules** (`/admin/crm-rules`):
   - `tender_owner_email`: the tender desk's email address.
   - `director_approvers`: add the stand-in approver's employee code.
   - `margin_viewers_ff`: add the three other named people's codes.
   - Check `margin_floors`, `head_approval_limit_inr`, `quote_validity_days`.
7. Godrej: run the script without arguments, send the list to
   management, then `--apply --ids <ids> --approver <code>`.
8. Leave `FEATURE_MARGIN_GATE` off until the floors have been compared
   with a week of real quotes; then turn it on in **Switches**.

## Tests

`tests/test_lead_vertical_and_task_visibility.py` (with leak checks),
`tests/test_lead_entry_rules.py`, `tests/test_margin_policy.py`, and a
new case in `tests/test_weekly_pack.py`. Each new guard was broken on
purpose to confirm a test fails. Full suite green.

## Not in this release

- B2 revision rule, B3 non-enquiry review list, E2 hour-based colours,
  E4 automatic ageing (60/120 days), E6 holiday calendars, F2 director
  digest, F3 conversation-number format, F7 client acknowledgement,
  A5 leaver reassignment, A6 HRMS sync, D3 entity default on every lead,
  I1–I3 Won evidence and ops hand-off mail, J3 no-login list.
- G1–G8 vendor master, VEN codes, route codes, minimum vendors and chase,
  sending rate requests — part 2, after the lists come back.
- GeM mail is still on the ingest blocklist (`email_ingest/parser.py`);
  tender leads are keyed in by the tender desk until that is reviewed.
