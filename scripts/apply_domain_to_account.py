"""
Add the confirmed domains to their accounts.

    .venv/bin/python scripts/apply_domain_to_account.py \\
        --file docs/exports/domain_to_account_2026_10_18.xlsx --check
    .venv/bin/python scripts/apply_domain_to_account.py \\
        --file ...xlsx --by DIR12010 --apply

`email_domains` decides whose mail reaches which account for ever
after, so this refuses rather than guesses:

    a blank `confirm account` is skipped — the commonest answer
    a name that matches no account, or more than one, is refused
      with the candidates printed
    free mail is refused outright. Nobody's account owns gmail.com
    a domain already listed on another account is refused, naming
      it — two accounts claiming one domain is a coin toss at
      intake

One transaction, and an audit row per account.
"""
import argparse
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Company, app, db                          # noqa: E402

ACTION = 'account.email_domain_added'
SHEET = 'Domains'


def read(path):
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True)
    if SHEET not in wb.sheetnames:
        raise SystemExit(f'No "{SHEET}" sheet in {path}. Sheets: '
                         f'{", ".join(wb.sheetnames)}')
    ws = wb[SHEET]
    header = [c.value for c in ws[2]]
    for need in ('domain', 'confirm account'):
        if need not in header:
            raise SystemExit(f'No "{need}" column. Columns: '
                             f'{", ".join(str(h) for h in header)}')
    out = []
    for row in ws.iter_rows(min_row=3):
        item = dict(zip(header, [c.value for c in row]))
        if item.get('domain'):
            out.append(item)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--file', required=True)
    ap.add_argument('--by', default='')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    if args.apply == args.check:
        raise SystemExit('Choose one: --check or --apply.')

    with app.app_context():
        from app.services import account_match, company_match

        print(f'database: {db.engine.url}')
        print(f'workbook: {args.file}')
        print(f'MODE:     {"APPLY — accounts will be written" if args.apply else "DRY RUN — nothing is written"}\n')

        rows = read(args.file)
        accounts = Company.query.all()
        by_name = {}
        for c in accounts:
            by_name.setdefault(company_match.norm(c.name), []).append(c)
        taken = {}
        for c in accounts:
            for d in (c.email_domains or []):
                taken.setdefault(str(d).lower().lstrip('@').strip(), c)

        plan, blank, refused = [], 0, []
        for item in rows:
            domain = str(item.get('domain') or '').strip().lower()
            want = str(item.get('confirm account') or '').strip()
            if not want:
                blank += 1
                continue
            if account_match.is_free_mail(domain):
                refused.append((domain, want, 'free mail — nobody owns it'))
                continue
            found = by_name.get(company_match.norm(want)) or []
            if len(found) != 1:
                refused.append((domain, want,
                                'no such account' if not found
                                else f'{len(found)} accounts share that name'))
                continue
            account = found[0]
            already = taken.get(domain)
            if already is not None and already.id != account.id:
                refused.append((domain, want,
                                f'already on "{already.name}" (#{already.id})'))
                continue
            if already is not None:
                continue                       # already right
            plan.append((domain, account))

        print(f'  rows on the sheet        {len(rows):>5}')
        print(f'  blank — left for later   {blank:>5}')
        print(f'  would be added           {len(plan):>5}')
        print(f'  refused                  {len(refused):>5}')
        for domain, want, why in refused:
            print(f'      {domain:<32} {want[:26]:<26} {why}')
        if plan:
            print('\n  to add:')
            for domain, account in plan:
                print(f'      {domain:<32} -> {account.name[:40]} '
                      f'(#{account.id})')

        if args.check:
            print('\nDRY RUN — nothing written. Re-run with --apply.')
            return

        from app.services import audit
        who = args.by.strip() or 'CRM-AUTO-ASSIGN-01'
        written = 0
        try:
            for domain, account in plan:
                existing = list(account.email_domains or [])
                if domain in [str(d).lower() for d in existing]:
                    continue
                account.email_domains = existing + [domain]
                audit.record(ACTION, 'Company', account.id,
                             old={'email_domains': existing},
                             new={'email_domains': account.email_domains},
                             actor=who,
                             reason=f'{domain} confirmed as this account '
                                    f'— CRM-AUTO-ASSIGN-01',
                             commit=False)
                written += 1
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'\n  !! ROLLED BACK: {type(exc).__name__}: '
                  f'{str(exc)[:200]}')
            raise SystemExit(1)
        print(f'\n  {written} domain(s) added, in one transaction, each '
              f'with an audit row.')
        print('  Re-run scripts/match_rate_report.py to see what it buys.')


if __name__ == '__main__':
    main()
