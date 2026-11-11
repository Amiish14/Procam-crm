"""Why this lead will not go to Quoted. READ ONLY.

CRM-WA-01b §1: "Find the lead (by sender overseas@…), and report
read-only: lead id, lead owner, linked account id/name/owner/vertical,
the user trying to quote, and the exact rule and message that refuse
the stage change to Quoted."

    .venv/bin/python scripts/2026_11_11_account_block.py --check \
          --sender overseas@sinofreight.com.cn
    .venv/bin/python scripts/2026_11_11_account_block.py --check \
          --lead 10842 --as EMPCODE
    .venv/bin/python scripts/2026_11_11_account_block.py --check \
          --stuck

WHAT IT ASKS, AND WHY IT ASKS IT TWICE

`account_gate.check` is the rule. It is now person-aware — an
ownership question about the account no longer refuses the lead's own
owner, its secondary PIC or anybody in its vertical — so "is this
lead blocked" has no answer until you say *for whom*. The report
therefore prints the verdict for every person with a claim on the
lead, and for a passer-by, side by side. If they differ, that
difference is the fix working.

`--as CODE` answers it for one named person, which is what to run
when somebody says they still cannot move a lead.

`--stuck` is the same question over the whole book: every open lead
the rule still refuses for its own owner. That list is the staffing
backlog, and it is the one worth clearing on Accounts → Account
check.

IT WRITES NOTHING

No `--apply`. Where a lead needs a named owner for its vertical, the
place to name one is Accounts → Account check, which writes the
`AccountVerticalOwner` row and its audit trail through the path that
already exists and already checks who may do it (D6). A second writer
here would be a second place for that check to be wrong.
"""
from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Company, Employee, Lead, app, db              # noqa: E402


def _rule(n=70):
    print('─' * n)


def find_by_sender(address):
    """Leads that arrived from this address, newest first."""
    wanted = (address or '').strip().lower()
    if not wanted:
        return []
    like = f'%{wanted}%'
    from sqlalchemy import or_
    return (Lead.query
            .filter(or_(db.func.lower(Lead.original_email_from).like(like),
                        db.func.lower(Lead.email).like(like)))
            .order_by(Lead.id.desc())
            .all())


def claimants(lead):
    """Everybody the rule now treats as having a claim on this lead.

    The lead's own two owner columns, plus the people whose employee
    record puts them in the lead's vertical. Codes read from the
    database, never written down here.
    """
    from app.services import account_gate

    out = []
    for code, role in ((lead.assigned_to, 'assigned_to'),
                       (lead.secondary_owner, 'secondary_owner')):
        code = (code or '').strip()
        if code and code not in [c for c, _r in out]:
            out.append((code, role))
    vertical = account_gate.vertical_of(lead)
    if vertical:
        for emp in Employee.query.filter_by(is_active=True).all():
            if not emp.emp_code or emp.emp_code in [c for c, _r in out]:
                continue
            if account_gate.worker_claim(lead, emp.emp_code):
                out.append((emp.emp_code, f'in {vertical}'))
    return out


def report_lead(lead, *, asking=None):
    from app.services import account_gate

    print(f'\n══ lead #{lead.id} ══')
    print(f'  company on the lead : {lead.company or "—"}')
    print(f'  stage               : {lead.stage or "—"}')
    print(f'  vertical (lead)     : {lead.procam_vertical or "—"}'
          f'  → master {account_gate.vertical_of(lead) or "—"}')
    print(f'  owner / Primary PIC : {lead.assigned_to or "— unowned"}')
    print(f'  secondary PIC       : {lead.secondary_owner or "—"}')
    print(f'  contact person      : {lead.pic or "—"}')
    print(f'  arrived from        : {lead.original_email_from or "—"}')

    account = account_gate.account_of(lead)
    print('\n  ── the linked account ──')
    if account is None:
        print('  none. The lead carries no company_id at all.')
    else:
        vertical = account_gate.vertical_of(lead)
        print(f'  id / name           : {account.id} / {account.name}')
        print(f'  account vertical    : {getattr(account, "vertical", "") or "—"}')
        print(f'  owner for {vertical or "?":<10}: '
              f'{account_gate.owner_for(account, vertical) or "— nobody named"}')
        _owners_everywhere(account)
        mismatch = account_gate.name_mismatch(lead)
        if mismatch:
            print(f'  !! the names disagree: the lead says '
                  f'"{mismatch[0]}", the account is "{mismatch[1]}". '
                  f'That is a Confirm account prompt on the lead, not a '
                  f'refusal — it never was one.')

    print('\n  ── the rule ──')
    print(f'  {account_gate.RULE}, app/services/account_gate.py')
    if not account_gate.enabled():
        print('  the flag is OFF on this database, so the rule refuses '
              'nothing here. If the user sees a refusal, they are on a '
              'database where it is on.')
    code, why = account_gate.reason_for(lead)
    print(f'  reason_for(lead)    : {code or "(nothing pending)"}'
          f'{"  — " + why if why else ""}')
    print(f'  an ownership question: '
          f'{"yes" if code in account_gate.OWNERSHIP_REASONS else "no"}'
          f'  (ownership never refuses somebody with a claim)')

    print('\n  ── who can move it to Quoted, and who cannot ──')
    people = claimants(lead)
    if asking:
        people = [(asking, 'asked for on the command line')] + [
            (c, r) for c, r in people if c != asking]
    if not people:
        print('  nobody has a claim: the lead is unowned and nobody is '
              'in its vertical.')
    for code_, role in people[:25]:
        refusal = account_gate.check(lead, 'Quoted', emp_code=code_)
        claim = account_gate.worker_claim(lead, code_)
        print(f'  {code_:<12} {role:<28} '
              f'{"REFUSED" if refusal else "may quote"}'
              f'{"  (" + claim + ")" if claim else ""}')
        if refusal:
            print(f'      {refusal}')
    passer_by = account_gate.check(lead, 'Quoted', emp_code='ZZZ-NOBODY')
    print(f'\n  a passer-by with no claim: '
          f'{"REFUSED" if passer_by else "may quote"}')
    if passer_by:
        print(f'      {passer_by}')
    print(f'\n  who can clear it    : {account_gate.resolver_for(lead)}')


def _owners_everywhere(account):
    """Every vertical this account is owned for — the Air/FF answer."""
    try:
        from app.models.assignment import AccountVerticalOwner
        rows = (AccountVerticalOwner.query
                .filter_by(account_id=account.id).all())
    except Exception as exc:                              # noqa: BLE001
        print(f'  (owner rows could not be read: {type(exc).__name__})')
        return
    if not rows:
        print('  owner rows          : none for any vertical')
        return
    print('  owner rows          :')
    for row in rows:
        print(f'    {(row.vertical or "—"):<22} {row.emp_code or "—":<12} '
              f'{getattr(row, "source", "") or ""}')


def report_stuck(limit=40):
    """Open leads the rule still refuses for the lead's own owner."""
    from app.services import account_gate, sales_rules

    if not account_gate.enabled():
        print('\n── still stuck ──\n  the flag is off here; nothing to '
              'count.')
        return
    rows = (Lead.query
            .filter(Lead.is_archived.isnot(True),
                    Lead.junk_deleted.isnot(True),
                    Lead.stage.in_(sales_rules.open_stages()))
            .all())
    stuck = []
    for lead in rows:
        owner = (lead.assigned_to or '').strip()
        if not owner:
            continue
        if account_gate.check(lead, 'Quoted', emp_code=owner):
            code, why = account_gate.reason_for(lead)
            stuck.append((lead, code, why))
    print(f'\n── open leads whose own owner still cannot quote: '
          f'{len(stuck)} ──')
    print('  After CRM-WA-01b these are the real ones: the customer is '
          'not identified, not merely unstaffed.')
    from collections import Counter
    for code, n in Counter(c for _l, c, _w in stuck).most_common():
        print(f'  {n:>5}  {code}')
    for lead, code, why in stuck[:limit]:
        print(f'    #{lead.id:<7} {(lead.company or "")[:34]:<34} '
              f'{lead.assigned_to or "":<12} {why}')
    if len(stuck) > limit:
        print(f'    … and {len(stuck) - limit} more')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true', required=True,
                    help='required: this script only ever reads')
    ap.add_argument('--sender', default=None, metavar='ADDRESS',
                    help='find the lead by the address it arrived from')
    ap.add_argument('--lead', type=int, default=None, metavar='ID')
    ap.add_argument('--as', dest='asking', default=None, metavar='CODE',
                    help='answer "can this person quote it" for one code')
    ap.add_argument('--stuck', action='store_true',
                    help='every open lead whose own owner is still refused')
    args = ap.parse_args(argv)

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('CRM-WA-01b — why a lead will not go to Quoted. READ ONLY.')
        _rule()
        asking = (args.asking or '').strip().upper() or None
        if asking and not Employee.query.filter_by(emp_code=asking).first():
            print(f'\n!! no employee in this CRM has the code {asking}. '
                  f'scripts/signin_check.py --code {asking} says more.')

        leads = []
        if args.lead:
            lead = db.session.get(Lead, args.lead)
            if lead is None:
                print(f'\nno lead #{args.lead}.')
                return 2
            leads.append(lead)
        if args.sender:
            found = find_by_sender(args.sender)
            if not found:
                print(f'\nno lead arrived from {args.sender!r}. Either it '
                      f'was never ingested — scripts/mail_ingest_status.py '
                      f'--check --from {args.sender} says which — or it '
                      f'was filed under a different address.')
            leads.extend(l for l in found if l not in leads)

        for lead in leads[:10]:
            report_lead(lead, asking=asking)
        if len(leads) > 10:
            print(f'\n… {len(leads) - 10} further lead(s) from that '
                  f'sender not shown; name one with --lead.')

        if args.stuck:
            report_stuck()

        if not leads and not args.stuck:
            print('\nnothing asked for. Give --sender, --lead or --stuck.')
        print()
        db.session.rollback()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
