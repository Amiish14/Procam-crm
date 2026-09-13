"""
How much real intake can auto-assign — read-only.

969 accounts and 339 with owners says nothing useful on its own: most
accounts never write. What matters is the share of mail that actually
arrives from an account configured well enough to assign itself.

This ranks sender domains by how much they have sent, resolves each to
an account, and reports the volume covered rather than the account
count. The unconfigured domains come out in volume order, so the list
is a worklist: fill in the top few and most of the intake assigns
itself.

Writes nothing.

    python scripts/assignment_coverage.py
    python scripts/assignment_coverage.py --top 40
    python scripts/assignment_coverage.py --days 90
"""
import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, EmailClassification                       # noqa: E402
from app.services import lead_intake_db as lidb                # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--top', type=int, default=25,
                    help='how many unconfigured domains to list')
    ap.add_argument('--days', type=int, default=365)
    args = ap.parse_args()

    with app.app_context():
        since = datetime.utcnow() - timedelta(days=args.days)
        rows = (EmailClassification.query
                .with_entities(EmailClassification.from_domain,
                               EmailClassification.from_addr,
                               EmailClassification.classification)
                .filter(EmailClassification.created_at >= since).all())
        if not rows:
            print('  No classifications in the window.')
            return 0

        # One sender per domain is enough to resolve the account, and
        # resolving 2,000 addresses one by one would be slow for no gain.
        volume = Counter()
        a_sender = {}
        leads = Counter()
        for domain, addr, klass in rows:
            if not domain:
                continue
            volume[domain] += 1
            a_sender.setdefault(domain, addr)
            if klass == 'A_new_lead':
                leads[domain] += 1

        covered = uncovered = unmapped = 0
        gaps = []
        for domain, n in volume.items():
            account, _how = (None, None)
            try:
                account, _how = lidb.resolve_account(a_sender[domain])
            except Exception:
                pass
            if account is None:
                unmapped += n
                gaps.append((n, leads[domain], domain, '(no account)'))
                continue
            primary, _secondary = lidb.owners_for(account)
            if primary:
                covered += n
            else:
                uncovered += n
                gaps.append((n, leads[domain], domain, account.name))

        total = covered + uncovered + unmapped
        pct = lambda n: f'{round(100 * n / total)}%' if total else '—'

        print(f'\n  {total} message(s) from {len(volume)} sender domain(s), '
              f'last {args.days} days\n')
        print(f'    assigns itself      {covered:>6}  {pct(covered)}')
        print(f'    account, no owner   {uncovered:>6}  {pct(uncovered)}')
        print(f'    no account at all   {unmapped:>6}  {pct(unmapped)}')

        if not gaps:
            print('\n  Every sender is configured. Nothing to do.')
            return 0

        gaps.sort(reverse=True)
        print(f'\n  The worklist — highest volume first. Configure these at '
              f'/accounts/owners:\n')
        print(f'    {"mail":>5} {"leads":>6}  {"domain":<34} account')
        running = 0
        for n, lead_n, domain, account in gaps[:args.top]:
            running += n
            print(f'    {n:>5} {lead_n:>6}  {domain:<34} {account}')
        share = round(100 * running / total) if total else 0
        print(f'\n  Those {min(args.top, len(gaps))} would cover {share}% of '
              f'all intake.')
        print('  "leads" is how many of that domain\'s messages the engine '
              'judged\n  a genuine enquiry — the column to prioritise by.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
