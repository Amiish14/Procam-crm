"""One lead the RFQ desk could not fix, and how many share its state.

CRM-MAIL-02 4 and 5. An air-freight RFQ from Fox Brasil: the desk set
the owner and a secondary PIC, the save was refused, and the lead
still reads "Nobody is accountable for this lead".

    read-only, and this is the one to run first:
    .venv/bin/python scripts/2026_11_12_fox_brasil.py --check
    .venv/bin/python scripts/2026_11_12_fox_brasil.py --check \
          --name "fox brasil"
    .venv/bin/python scripts/2026_11_12_fox_brasil.py --check --states

    then, with the numbers that printed:
    .venv/bin/python scripts/2026_11_12_fox_brasil.py --apply \
          --lead 12731 --account 884 --vertical "Air Freight" \
          --owner <SEA-FREIGHT-HEAD-CODE> --second <AIR-FREIGHT-CODE>

WHY THE CODES ARE ON THE COMMAND LINE

Because a person's employee code is data, and `--check` prints the
candidates with their names so whoever runs it can see who they are
about to make accountable. Nothing here holds a name, a code or a
customer; `--name` defaults to the one in the ticket and takes any
other.

WHAT --check SHOWS

    the lead          its vertical, its owner, its second owner, the
                      account it is linked to, and the exact sentence
                      the gate answers for it today — for the RFQ
                      desk, for the owner, and for a passer-by.
    the accounts      every account whose name resembles the one you
                      asked about, with its vertical, its owner per
                      vertical and how many leads it carries. A
                      spelling variant is the usual reason a lead is
                      on the wrong one, and it is also why linking it
                      is a decision rather than a lookup.

WHAT --apply DOES, IN ONE TRANSACTION

Links the account (through `lead_account.link`, which writes both
histories), sets the vertical, and sets the two PICs through
`lead_assignment.assign`, which writes the assignment history row and
notifies. Each of the four is optional: pass only what is changing.
Every one of them leaves an audit row, and the whole thing rolls back
together if any part refuses.

It refuses to guess. No `--account` means the link is not touched; an
employee code that is not in the CRM, or not active, stops the run
before anything is written.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import Company, Employee, Lead, app, db              # noqa: E402

ACTOR = 'CRM-MAIL-02'
DEFAULT_NAME = 'fox brasil'


def _rule(n=70):
    print('─' * n)


# ── finding it ────────────────────────────────────────────
def leads_named(name):
    """Leads whose company, sender or subject mentions this name."""
    like = f'%{(name or "").strip().lower()}%'
    return (Lead.query
            .filter(db.or_(
                db.func.lower(db.func.coalesce(Lead.company, '')).like(like),
                db.func.lower(db.func.coalesce(Lead.email, '')).like(like),
                db.func.lower(db.func.coalesce(
                    Lead.original_email_from, '')).like(like),
                db.func.lower(db.func.coalesce(
                    Lead.original_email_subject, '')).like(like)))
            .order_by(Lead.id.desc())
            .all())


def accounts_named(name):
    """Accounts whose name, alias or domain resembles this one.

    `company_match.norm` for the comparison, so "Fox Brasil",
    "FOX BRASIL LTDA" and "Fox Brazil" are judged the way the matcher
    judges them rather than by a second rule written here.
    """
    from app.services import company_match

    wanted = company_match.norm(name or '')
    like = f'%{(name or "").strip().lower()}%'
    found = {}
    for account in Company.query.filter(
            db.func.lower(db.func.coalesce(Company.name, '')).like(like)
    ).all():
        found[account.id] = (account, 'the name contains it')
    if wanted:
        for account in Company.query.all():
            if account.id in found:
                continue
            names = [account.name or '']
            names += [str(a) for a in (getattr(account, 'aliases', None) or [])]
            for other in names:
                if other and company_match.norm(other) == wanted:
                    found[account.id] = (account, 'the normalised name '
                                                  'is the same')
                    break
    return list(found.values())


def show_lead(lead):
    from app.services import account_gate, lead_people, lead_team

    print(f'\n══ lead #{lead.id} ══')
    print(f'  company on the lead : {lead.company or "—"}')
    print(f'  stage               : {lead.stage or "—"}')
    print(f'  vertical (the lead) : {lead.procam_vertical or "— none"}')
    print(f'  owner / Primary PIC : {lead.assigned_to or "— nobody"}')
    print(f'  secondary PIC       : {lead.secondary_owner or "— nobody"}')
    print(f'  arrived from        : {lead.original_email_from or "—"}')
    print(f'  subject             : '
          f'{(lead.original_email_subject or "")[:70]!r}')

    account = account_gate.account_of(lead)
    print('\n  ── the account it is linked to ──')
    if account is None:
        print('  none — the lead carries no company_id.')
    else:
        print(f'  id / name           : {account.id} / {account.name}')
        print(f'  the account\'s own vertical: '
              f'{getattr(account, "vertical", "") or "—"}')
        _owner_rows(account)

    print('\n  ── what the gate answers today ──')
    print(f'  the lead\'s vertical as the gate reads it: '
          f'{account_gate.vertical_of(lead) or "— none"}')
    code, why = account_gate.reason_for(lead)
    print(f'  reason              : {code or "(nothing pending)"}'
          f'{"  — " + why if why else ""}')
    for who, label in _people_to_try(lead):
        refusal = account_gate.check(lead, 'Quoted', emp_code=who)
        claim = account_gate.worker_claim(lead, who)
        allowed, why_set = lead_people.may_set_people(lead, who)
        print(f'  {who:<12} {label:<22} '
              f'{"REFUSED" if refusal else "may quote"}'
              f'{"  (" + claim + ")" if claim else ""}')
        print(f'      may set the owner: '
              f'{"yes — " + why_set if allowed else "no"}')

    team = lead_team.summary(lead) if hasattr(lead_team, 'summary') else {}
    if team.get('unowned'):
        print('\n  !! nobody is accountable for this lead: no owner and '
              'no second owner. That is the state §2 is about, and it '
              'is also why the ownership exemption could not help — '
              'there is no ownership to read.')


def _people_to_try(lead):
    """The lead's own people, the vertical's head, and the RFQ desk."""
    from app.services import account_gate, lead_people

    out, seen = [], set()

    def add(code, label):
        code = (code or '').strip().upper()
        if code and code not in seen:
            seen.add(code)
            out.append((code, label))

    add(lead.assigned_to, 'the lead owner')
    add(lead.secondary_owner, 'the secondary PIC')
    vertical = account_gate.vertical_of(lead)
    for code in sorted(lead_people.head_codes(vertical)):
        add(code, f'head of {vertical}')
    for code in sorted(lead_people.desk_codes(vertical)):
        add(code, 'the RFQ desk')
    add('ZZZ-NOBODY', 'a passer-by')
    return out


def _owner_rows(account):
    try:
        from app.models.assignment import AccountVerticalOwner
        rows = (AccountVerticalOwner.query
                .filter_by(account_id=account.id).all())
    except Exception as exc:                              # noqa: BLE001
        print(f'  (owner rows unreadable: {type(exc).__name__})')
        return
    if not rows:
        print('  owner rows          : none, for any vertical')
        return
    print('  owner rows          :')
    for row in rows:
        emp = Employee.query.filter_by(emp_code=row.emp_code).first()
        state = ('not in the employee master' if emp is None
                 else ('ACTIVE' if getattr(emp, 'is_active', False)
                       else 'INACTIVE'))
        print(f'    {(row.vertical or "—"):<20} '
              f'{(row.emp_code or "—"):<12} {state}')


def show_accounts(name):
    print(f'\n══ accounts that look like {name!r} ══')
    found = accounts_named(name)
    if not found:
        print('  none. If the customer has no account at all, that is '
              'the thing to fix first — Accounts → Account check can '
              'create one.')
        return
    for account, how in found:
        leads = Lead.query.filter(Lead.company_id == account.id).count()
        active = 'active' if getattr(account, 'is_active', False) else 'INACTIVE'
        print(f'\n  #{account.id} {account.name}  ({how}, {active})')
        print(f'    vertical: {getattr(account, "vertical", "") or "—"} · '
              f'{leads} lead(s) · domains: '
              f'{", ".join(getattr(account, "email_domains", None) or []) or "—"}')
        _owner_rows(account)
    if len(found) > 1:
        print('\n  !! more than one. A spelling variant is the usual '
              'reason a lead is on the wrong account, and merging two '
              'accounts is /admin/account-duplicates — not this '
              'script.')


# ── §5: who else is in this state ──────────────────────────────
def states():
    """Counts only, as §5 asks. Two states, counted separately."""
    from app.services import account_gate, sales_rules

    rows = (Lead.query
            .filter(Lead.is_archived.isnot(True),
                    Lead.junk_deleted.isnot(True),
                    Lead.merged_into_id.is_(None),
                    Lead.stage.in_(sales_rules.open_stages()))
            .all())
    people = {e.emp_code: e for e in Employee.query.all() if e.emp_code}
    inactive_owner = 0
    vertical_mismatch = 0
    no_lead_vertical = 0
    unowned = 0
    by_vertical = Counter()
    for lead in rows:
        account = account_gate.account_of(lead)
        lead_vertical = account_gate.vertical_of(lead)
        if not lead_vertical:
            no_lead_vertical += 1
        if not (lead.assigned_to or lead.secondary_owner):
            unowned += 1
        if account is None:
            continue
        owner = account_gate.owner_for(account, lead_vertical)
        if owner:
            emp = people.get(owner.strip().upper())
            if emp is None or not getattr(emp, 'is_active', False):
                inactive_owner += 1
                by_vertical[lead_vertical or 'no vertical'] += 1
        account_vertical = (getattr(account, 'vertical', '') or '').strip()
        if (account_vertical and lead_vertical
                and account_vertical.strip().lower()
                != lead_vertical.strip().lower()):
            vertical_mismatch += 1
    return {
        'open': len(rows),
        'inactive_owner': inactive_owner,
        'vertical_mismatch': vertical_mismatch,
        'no_lead_vertical': no_lead_vertical,
        'unowned': unowned,
        'by_vertical': by_vertical,
    }


def report_states():
    out = states()
    print(f'\n══ §5 — how many leads are in the same state ══')
    print(f'  {out["open"]} open lead(s) looked at.')
    print(f'  {out["inactive_owner"]} have an account owner for their own '
          f'vertical who is INACTIVE or not in the employee master. '
          f'Every notification about those leads is undeliverable.')
    print(f'  {out["vertical_mismatch"]} are linked to an account whose '
          f'own default vertical is a different one. That is normal for '
          f'a customer who buys more than one service — it is only a '
          f'fault when a rule reads the account\'s vertical instead of '
          f'the lead\'s, which is what §3 fixes.')
    print(f'  {out["no_lead_vertical"]} carry no vertical of their own, '
          f'so no owner question can be asked about them at all.')
    print(f'  {out["unowned"]} have neither an owner nor a second owner.')
    if out['by_vertical']:
        print('\n  the inactive-owner ones, by the lead\'s vertical:')
        for vertical, n in out['by_vertical'].most_common():
            print(f'    {n:>5}  {vertical}')
    print('\n  Counts only, as asked. The lead-by-lead list is '
          'scripts/2026_11_11_account_block.py --check --stuck and '
          '/admin/account-link.')


# ── the one lead ──────────────────────────────────────
def _employee_or_stop(code, what):
    if not code:
        return None
    emp = Employee.query.filter_by(emp_code=code.strip().upper()).first()
    if emp is None:
        print(f'\n  !! no employee has the code {code!r} ({what}). '
              f'Nothing written. scripts/signin_check.py --code {code} '
              f'says more.')
        raise SystemExit(2)
    if not getattr(emp, 'is_active', False):
        print(f'\n  !! {code} ({what}) is deactivated in the CRM. '
              f'Making a deactivated person accountable is how a lead '
              f'goes quiet. Nothing written.')
        raise SystemExit(2)
    return emp


def apply(lead, *, account_id=None, vertical=None, owner=None,
          second=None):
    from app.services import audit, lead_account, lead_assignment

    owner_emp = _employee_or_stop(owner, 'the owner')
    second_emp = _employee_or_stop(second, 'the secondary PIC')
    account = (db.session.get(Company, account_id) if account_id else None)
    if account_id and account is None:
        print(f'\n  !! no account #{account_id}. Nothing written.')
        raise SystemExit(2)

    done = []
    try:
        if account is not None and lead.company_id != account.id:
            ok, detail = lead_account.link(
                lead, account, actor=ACTOR, learn=False,
                reason=('CRM-MAIL-02 §4: linked to the account the RFQ '
                        'desk identified')[:400])
            if not ok:
                print(f'\n  !! the link was refused: {detail}')
                raise SystemExit(2)
            done.append(f'linked to #{account.id} {account.name}')

        if vertical is not None:
            was = lead.procam_vertical or ''
            if (vertical or '').strip() != was.strip():
                lead.procam_vertical = (vertical or '').strip() or None
                audit.record('lead.vertical_changed', 'lead', lead.id,
                             old={'procam_vertical': was},
                             new={'procam_vertical': lead.procam_vertical},
                             actor=ACTOR,
                             reason=('CRM-MAIL-02 §4: the enquiry is '
                                     'air freight; the account\'s own '
                                     'default vertical is not the '
                                     'lead\'s'))
                done.append(f'vertical {was or "none"} → '
                            f'{lead.procam_vertical}')

        if owner_emp is not None or second_emp is not None:
            ok, err = lead_assignment.assign(
                lead,
                primary_code=(owner_emp.emp_code if owner_emp else None),
                secondary_code=(second_emp.emp_code if second_emp
                                else None),
                actor=ACTOR,
                note='CRM-MAIL-02 §4: as the RFQ desk entered them')
            if not ok:
                print(f'\n  !! the assignment was refused: {err}')
                raise SystemExit(2)
            if owner_emp is not None:
                done.append(f'owner → {owner_emp.emp_code}')
            if second_emp is not None:
                done.append(f'second PIC → {second_emp.emp_code}')

        db.session.commit()
    except SystemExit:
        db.session.rollback()
        raise
    except Exception as exc:                              # noqa: BLE001
        db.session.rollback()
        print(f'\n  !! ROLLED BACK: {type(exc).__name__}: {str(exc)[:200]}')
        raise SystemExit(1)

    print(f'\n{len(done)} change(s) on lead #{lead.id}:')
    for line in done:
        print(f'  {line}')
    if not done:
        print('  nothing — it already reads that way.')
    from app.services import account_gate
    code, why = account_gate.reason_for(lead)
    print(f'\nthe gate now answers: {code or "nothing pending"}'
          f'{"  — " + why if why else ""}')
    if code:
        print(f'  who can clear it: {account_gate.resolver_for(lead)}')
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true',
                    help='report only. The default.')
    ap.add_argument('--states', action='store_true',
                    help='§5: counts of other leads in the same state')
    ap.add_argument('--name', default=DEFAULT_NAME, metavar='TEXT')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--lead', type=int, default=None, metavar='ID')
    ap.add_argument('--account', type=int, default=None, metavar='ID')
    ap.add_argument('--vertical', default=None, metavar='NAME')
    ap.add_argument('--owner', default=None, metavar='CODE')
    ap.add_argument('--second', default=None, metavar='CODE')
    args = ap.parse_args(argv)

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('CRM-MAIL-02 — the lead the RFQ desk could not fix.'
              + ('' if args.apply else ' REPORT ONLY.'))
        _rule()

        if args.apply:
            if not args.lead:
                print('\n--apply needs --lead ID.')
                return 2
            lead = db.session.get(Lead, args.lead)
            if lead is None:
                print(f'\nno lead #{args.lead}.')
                return 2
            show_lead(lead)
            apply(lead, account_id=args.account, vertical=args.vertical,
                  owner=args.owner, second=args.second)
            return 0

        if args.lead:
            lead = db.session.get(Lead, args.lead)
            if lead is None:
                print(f'\nno lead #{args.lead}.')
                return 2
            show_lead(lead)
        else:
            found = leads_named(args.name)
            if not found:
                print(f'\nno lead mentions {args.name!r}.')
            for lead in found[:6]:
                show_lead(lead)
            if len(found) > 6:
                print(f'\n… {len(found) - 6} more. Name one with --lead.')
        show_accounts(args.name)

        if args.states:
            report_states()

        print('\nDRY RUN — nothing written. The --apply line in the '
              'docstring above takes the ids and codes printed here.')
        db.session.rollback()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
