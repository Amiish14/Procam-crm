"""
Accounts that are not companies: "Unknown", "New Lead" and friends.

    .venv/bin/python scripts/placeholder_accounts.py
    .venv/bin/python scripts/placeholder_accounts.py --names "Unknown,New Lead"

READ-ONLY. Nothing here is changed, renamed, merged or moved.

These came out of the owner-seeding disagreement list: two of the
eleven rows named an account "Unknown" and another "New Lead". An
account with a placeholder name is worse than a missing one —
every lead filed under it looks placed, so it never reaches
Account check, and the history of several real customers is
pooled under one meaningless row.

For each lead the real company is proposed from its own mail, in
this order:
    the sender's domain, where it is not free mail
    a GSTIN in the body, matched to an account on file
    the signature block, where one can be read
    the typed company name on the lead, if it says anything

Proposals only. Merging them is a decision with consequences for
history, and it needs a person who knows the customers.
"""
import argparse
import os
import sys
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Company, Lead, app, db                    # noqa: E402

#: Names that describe the row's emptiness rather than a company.
DEFAULT_NAMES = ('Unknown', 'New Lead', 'N/A', 'NA', 'Test',
                 'Customer', 'Client', '-')


def placeholder_accounts(names):
    wanted = {n.strip().lower() for n in names if n.strip()}
    return [c for c in Company.query.all()
            if (c.name or '').strip().lower() in wanted]


def propose(lead):
    """(suggestion, how) — the real company, from the lead's mail."""
    from app.services import account_match, account_suggest

    sender = account_suggest.sender_of(lead)
    domain = account_match.domain_of(sender)
    text = (f'{lead.forwarded_subject or lead.original_email_subject or ""}'
            f'\n{lead.original_email_body or lead.notes or ""}')[:4000]

    # An account the matcher can already name is the strongest
    # answer: it means this lead belongs to a real row on file.
    rows = account_match.candidates(sender, company_name=lead.company,
                                    text=text)
    real = [r for r in rows
            if (r['name'] or '').strip().lower() not in
            {n.lower() for n in DEFAULT_NAMES}]
    if real:
        return real[0]['name'], f"matches on {real[0]['rule']}"

    if domain and not account_match.is_free_mail(domain):
        return domain, 'the sender domain, no account on file yet'

    try:
        from app.services import lead_intake as li
        for number in li.gstins(text):
            hit = Company.query.filter(Company.gstin == number).first()
            if hit is not None:
                return hit.name, f'GSTIN {number}'
            return f'(GSTIN {number})', 'a GSTIN with no account yet'
    except Exception:                                     # noqa: BLE001
        pass

    typed = (lead.company or '').strip()
    if typed and typed.lower() not in {n.lower() for n in DEFAULT_NAMES}:
        return typed, 'the name typed on the lead'

    if sender:
        return f'({sender})', 'nothing but the sender address'
    return '', 'nothing to go on — this one needs a person'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--names', default=','.join(DEFAULT_NAMES))
    args = ap.parse_args()
    names = args.names.split(',')

    with app.app_context():
        from app.services import account_gate

        print(f'database: {db.engine.url}')
        print('READ-ONLY — nothing is changed.\n')

        accounts = placeholder_accounts(names)
        if not accounts:
            print('  no placeholder-named accounts found')
            return

        grand = 0
        for account in sorted(accounts, key=lambda c: c.name or ''):
            leads = (Lead.query
                     .filter(Lead.company_id == account.id)
                     .order_by(Lead.id.desc()).all())
            print(f'  ── #{account.id} "{account.name}" — '
                  f'{len(leads)} lead(s)')
            if not leads:
                print('      no leads. An empty placeholder: safe to '
                      'retire once somebody confirms nothing points '
                      'at it.\n')
                continue
            grand += len(leads)
            by_proposal = defaultdict(list)
            for lead in leads:
                suggestion, how = propose(lead)
                by_proposal[(suggestion, how)].append(lead)

            for (suggestion, how), group in sorted(
                    by_proposal.items(), key=lambda kv: -len(kv[1])):
                print(f'      {len(group):>3}  -> '
                      f'{suggestion or "(nothing)":<38} {how}')
                for lead in group[:4]:
                    vertical = account_gate.vertical_of(lead) or '—'
                    print(f'            #{lead.id:<7}'
                          f'{(lead.stage or ""):<18}{vertical:<20}'
                          f'{(lead.email or lead.original_email_from or "")[:34]}')
                if len(group) > 4:
                    print(f'            … and {len(group) - 4} more')
            print()

        print(f'  {grand} lead(s) sit under a placeholder account in '
              f'total.')
        print('  Nothing was changed. Merging these affects history '
              'for real customers, so the proposals above are for a '
              'person who knows them to act on.')


if __name__ == '__main__':
    main()
