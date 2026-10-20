"""
FF-LINK-01 — give every open Freight Forwarding lead its account.

    .venv/bin/python scripts/ff_link_accounts.py --check
    .venv/bin/python scripts/ff_link_accounts.py --apply

Why this has to happen before FF goes on
    The vendor masking finds a client's contacts through the account
    link. A lead with no `company_id` has no contacts to find, so a
    rate request about it can carry the buyer's name and mobile
    straight to a carrier — the leak check still catches the company
    name, but not a person the CRM cannot see.

Matching is the existing service, not a second one
    `app.services.account_match.resolve` already knows the order:
    what somebody corrected by hand, then the contact table, then a
    learned domain, then the website, then the name. Writing another
    matcher here would be a second opinion that disagrees with the
    first somewhere, and nobody would know which had filed a lead.

Three answers, not two
    `resolve` returns nothing both when there is no candidate and
    when there are several, because it refuses to guess. That is
    right for it and useless for a report, so the ambiguous ones are
    worked out separately and listed with every candidate. Only the
    unambiguous are ever written.
"""
import argparse
import os
import sys
from collections import Counter
from datetime import date, datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import (Company, Lead, STAGES_TERMINAL, app, db)  # noqa: E402
from app.services import (account_match, company_match,     # noqa: E402
                          ff_privacy, lead_mode)

MATCHED, REVIEW = 'matched', 'needs a look'
AMBIGUOUS, NONE = 'ambiguous', 'no match'

#: Rules that identify a company rather than infer it. A person
#: corrected it, or the exact address is on a contact record.
TRUSTED_RULES = ('learned email', 'learned domain', 'contact email',
                 'company name')


def _names_agree(lead_name, account_name):
    """Whether these two are plausibly the same company.

    A shared email domain is not the same thing as a shared company.
    `renew.com` carries both RENEW POWER and RENEW PHOTOVOLTAICS, and
    an account auto-named from a domain label — there are accounts
    here called "Mail" and "Asia", created from `mail.oyaso.com` and
    `asia.meap.com` — matches by domain perfectly while naming
    nobody. A domain match whose names do not resemble each other is
    a suggestion, not a fact.
    """
    a = company_match.norm(lead_name or '')
    b = company_match.norm(account_name or '')
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    # One name inside the other: "bemlltd" in "bemllimited" does not
    # hold, but "agunsa" in "agunsachile" does. Require the shared
    # part to be most of the shorter name.
    return len(shorter) >= 4 and shorter in longer


def _candidates(lead):
    """Every account this lead could plausibly belong to.

    Only used to tell "nothing" apart from "several". The decision
    itself stays with `resolve`.
    """
    out = {}
    addr = account_match.normalise_address(lead.email)
    domain = account_match.domain_of(addr)
    if domain and not account_match.is_free_mail(domain):
        for c in Company.query.filter(
                Company.is_active.isnot(False)).all():
            for d in (c.email_domains or []):
                if (d or '').strip().lstrip('@').lower() == domain:
                    out[c.id] = c
    want = company_match.norm(lead.company or '')
    if want and len(want) >= 3:
        for c in Company.query.filter(
                Company.is_active.isnot(False)).all():
            if company_match.norm(c.name) == want:
                out[c.id] = c
    return list(out.values())


def survey():
    """(rows, buckets) for every open FF lead with no account."""
    leads = (Lead.query
             .filter(Lead.procam_vertical == lead_mode.VERTICAL,
                     Lead.stage.notin_(STAGES_TERMINAL),
                     Lead.is_archived.isnot(True),
                     Lead.company_id.is_(None))
             .order_by(Lead.id.asc()).all())
    rows = []
    for l in leads:
        account, rule = account_match.resolve(
            l.email, company_name=l.company, count_hit=False)
        if account is not None:
            trusted = (rule in TRUSTED_RULES
                       or _names_agree(l.company, account.name))
            rows.append({'lead': l,
                         'verdict': MATCHED if trusted else REVIEW,
                         'account': account, 'rule': rule,
                         'candidates': [account]})
            continue
        cands = _candidates(l)
        if len(cands) > 1:
            rows.append({'lead': l, 'verdict': AMBIGUOUS,
                         'account': None, 'rule': 'several candidates',
                         'candidates': cands})
        else:
            rows.append({'lead': l, 'verdict': NONE, 'account': None,
                         'rule': rule or 'unmapped', 'candidates': []})
    return rows, Counter(r['verdict'] for r in rows)


def unprotected_accounts():
    """FF accounts the masking cannot protect without an alias.

    Asked of the masking rule itself rather than a word list kept
    beside it: `variants()` is what decides whether a name can be
    redacted, so a name it reduces to nothing is a name that reaches
    a vendor intact, whatever a separate list might say.
    """
    ids = {l.company_id for l in Lead.query.filter(
        Lead.procam_vertical == lead_mode.VERTICAL,
        Lead.company_id.isnot(None)).all() if l.company_id}
    out = []
    for c in Company.query.filter(Company.id.in_(ids or {0})).all():
        if not ff_privacy.variants(c.name):
            out.append(c)
    return sorted(out, key=lambda c: (c.name or '').lower())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--export-review', dest='export_review',
                    action='store_true',
                    help='write the "needs a look" rows to '
                         'docs/exports/ and stop')
    args = ap.parse_args()
    if args.export_review:
        args.check = True
    if args.apply == args.check:
        raise SystemExit('Choose one: --check or --apply.')

    with app.app_context():
        print(f'database: {db.engine.url}')
        print(f'MODE:     {"APPLY" if args.apply else "DRY RUN — nothing is written"}\n')

        rows, buckets = survey()
        total_ff = Lead.query.filter(
            Lead.procam_vertical == lead_mode.VERTICAL,
            Lead.stage.notin_(STAGES_TERMINAL),
            Lead.is_archived.isnot(True)).count()
        linked = total_ff - len(rows)

        print(f'  open Freight Forwarding leads   {total_ff:>5}')
        print(f'  already linked to an account    {linked:>5}')
        print(f'  unlinked                        {len(rows):>5}')
        print()
        for verdict in (MATCHED, REVIEW, AMBIGUOUS, NONE):
            print(f'  {verdict:<12} {buckets.get(verdict, 0):>5}')

        for verdict, limit in ((MATCHED, 40), (REVIEW, 40),
                               (AMBIGUOUS, 40), (NONE, 25)):
            group = [r for r in rows if r['verdict'] == verdict]
            if not group:
                continue
            print(f'\n  === {verdict} ({len(group)})')
            if verdict in (MATCHED, REVIEW):
                print('  by rule:', dict(Counter(r['rule']
                                                 for r in group)))
            if verdict == REVIEW:
                print('  Matched on a shared email domain while the '
                      'names disagree. Not written — a domain is not '
                      'a company.')
            for r in group[:limit]:
                l = r['lead']
                print(f'    #{l.id:<6} {(l.company or "")[:34]:<34} '
                      f'{(l.email or "")[:28]:<28} {l.stage[:16]:<16}')
                if verdict in (MATCHED, REVIEW):
                    print(f'            -> {r["account"].name[:48]} '
                          f'(#{r["account"].id}, {r["rule"]})')
                elif verdict == AMBIGUOUS:
                    for c in r['candidates'][:4]:
                        print(f'            ? {c.name[:48]} (#{c.id})')
            if len(group) > limit:
                print(f'    … and {len(group) - limit} more')

        bare = unprotected_accounts()
        print(f'\n  === accounts the masking cannot protect '
              f'({len(bare)})')
        if bare:
            print('  A name that reduces to nothing distinctive reaches '
                  'a vendor intact. Add an alias in the account record.')
            for c in bare:
                print(f'    #{c.id:<6} {c.name}')
        else:
            print('  None — every FF account name has something '
                  'distinctive in it.')

        if args.export_review:
            path = _write_review(
                [r for r in rows if r['verdict'] == REVIEW])
            print(f'\n  {path}')
            print('  Nothing was written to the database.')
            return

        if args.check:
            print('\nDRY RUN — nothing written. Re-run with --apply to '
                  'link the matched only.')
            return

        from app.services import audit
        to_write = [r for r in rows if r['verdict'] == MATCHED]
        try:
            for r in to_write:
                lead, acct = r['lead'], r['account']
                lead.company_id = acct.id
                audit.record('lead.account_linked', 'lead', str(lead.id),
                             old={'company_id': None},
                             new={'company_id': acct.id,
                                  'account': acct.name,
                                  'rule': r['rule']},
                             actor='FF-LINK-01',
                             reason=f'Linked by {r["rule"]} before FF '
                                    f'goes on, so vendor masking can '
                                    f'find the client\'s contacts.')
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'\n  !! ROLLED BACK: {type(exc).__name__}: '
                  f'{str(exc)[:200]}')
            raise SystemExit(1)

        print(f'\n  {len(to_write)} lead(s) linked, in one transaction, '
              f'each with an audit row naming the rule.')
        print(f'  {buckets.get(REVIEW, 0)} needing a look, '
              f'{buckets.get(AMBIGUOUS, 0)} ambiguous and '
              f'{buckets.get(NONE, 0)} unmatched were not touched.')


def _write_review(rows):
    """The rows a person has to judge, as a sheet they can fill in."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    out = os.path.join(_ROOT, 'docs', 'exports',
                       f'ff_link_review_{date.today().isoformat()}.xlsx')
    os.makedirs(os.path.dirname(out), exist_ok=True)

    wb = Workbook()
    ws = wb.active
    ws.title = 'Needs a look'
    ws.append(['Freight Forwarding leads: which account?'])
    ws['A1'].font = Font(bold=True, size=14)
    ws.append([f'{len(rows)} lead(s) matched a suggested account on a '
               f'shared email domain while the names disagree. A '
               f'domain is not a company — renew.com carries two of '
               f'them, and accounts here were auto-named from domain '
               f'labels. Put the right account in the last column, or '
               f'"create new".'])
    ws.append([])
    cols = ['Lead', 'Company as typed', 'Sender', 'Stage', 'Owner',
            'Suggested account', 'Suggested id', 'Correct account']
    ws.append(cols)
    for cell in ws[4]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='1F2937')

    from app import Employee
    names = {e.emp_code: e.name for e in Employee.query.all()}
    for r in rows:
        l, a = r['lead'], r['account']
        ws.append([l.id, l.company or '', l.email or '', l.stage or '',
                   names.get(l.assigned_to, l.assigned_to or ''),
                   a.name if a else '', a.id if a else '', ''])

    # The dropdown: every active account, plus "create new". Excel
    # refuses a formula-1 list over about 255 characters, so the
    # choices live on their own sheet and the validation points at
    # the range.
    choices = wb.create_sheet('Accounts')
    choices.append(['create new'])
    choices.append(['leave unlinked'])
    for c in (Company.query.filter(Company.is_active.isnot(False))
              .order_by(Company.name.asc()).all()):
        choices.append([c.name])
    choices.sheet_state = 'hidden'
    last = choices.max_row
    dv = DataValidation(
        type='list', formula1=f'=Accounts!$A$1:$A${last}',
        allow_blank=True, showDropDown=False)
    # openpyxl writes `allowBlank="0"` if the camelCase spelling is
    # used; snake_case is the real parameter. Getting this wrong
    # produces a sheet that refuses every empty cell.
    # `ws.max_row` is the header row when there is nothing to judge,
    # and `H5:H4` is backwards — openpyxl raises rather than writes
    # it. An empty sheet is the normal end state of this exercise,
    # so it has to come out readable.
    ws.add_data_validation(dv)
    dv.add(f'H5:H{max(ws.max_row, 5)}')

    for col, w in (('A', 8), ('B', 36), ('C', 32), ('D', 16),
                   ('E', 22), ('F', 34), ('G', 12), ('H', 36)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = 'A5'
    wb.save(out)
    return out


if __name__ == '__main__':
    main()
