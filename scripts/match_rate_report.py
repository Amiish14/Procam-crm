"""
What the new matcher would have done to the last month of mail.

READ-ONLY. Replays the senders in the ingest log through both
matchers and prints the rates side by side, with which rule fired.

    .venv/bin/python scripts/match_rate_report.py --days 30
    .venv/bin/python scripts/match_rate_report.py --days 30 --csv /tmp/m.csv

The old matcher is `lead_intake_db.resolve_account`: contact email,
the account's domain list, a LIKE against the website, GSTIN, then
company name. The measured rate was 48 per cent, and three of those
five rules never fired once.

Nothing is written, including the learned-link hit counters.
"""
import argparse
import csv
import os
import sys
from collections import Counter
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import app, db                                   # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=30)
    ap.add_argument('--csv', default='')
    args = ap.parse_args()

    with app.app_context():
        from app.models.ingest_log import MailIngestLog
        from app.services import account_match
        from app.services import lead_intake_db as old

        print(f'database: {db.engine.url}')
        print('MODE:     READ-ONLY — nothing is written\n')

        since = datetime.utcnow() - timedelta(days=args.days)
        rows = (MailIngestLog.query
                .filter(MailIngestLog.received_at >= since)
                .order_by(MailIngestLog.received_at).all())
        print(f'{len(rows)} messages in the last {args.days} days')

        before, after, pairs = Counter(), Counter(), []
        gained = []
        for r in rows:
            sender = (r.from_addr or '').strip()
            if not sender or '@' not in sender:
                continue
            subject = r.subject or ''
            try:
                _a, old_why = old.resolve_account(sender,
                                                  text_for_gstin=subject)
            except Exception:
                db.session.rollback()
                old_why = 'error'
            try:
                acct, new_why = account_match.resolve(
                    sender, text=subject, count_hit=False)
            except Exception:
                db.session.rollback()
                acct, new_why = None, 'error'
            before[old_why] += 1
            after[new_why] += 1
            pairs.append((sender, old_why, new_why,
                          getattr(acct, 'name', '') or ''))
            if old_why == 'unmapped' and new_why != 'unmapped':
                gained.append((sender, new_why,
                               getattr(acct, 'name', '') or ''))

        total = sum(before.values()) or 1
        old_hit = total - before.get('unmapped', 0)
        new_hit = total - after.get('unmapped', 0)

        print(f'\n{"":<30} {"before":>8} {"after":>8}')
        print(f'{"matched to an account":<30} {old_hit:>8} {new_hit:>8}')
        print(f'{"unmapped":<30} {before.get("unmapped", 0):>8} '
              f'{after.get("unmapped", 0):>8}')
        print(f'{"match rate":<30} {100 * old_hit // total:>7}% '
              f'{100 * new_hit // total:>7}%')
        print(f'{"newly matched":<30} {"":>8} {len(gained):>8}')

        print('\n  which rule fired — before:')
        for why, n in before.most_common():
            print(f'      {why:<28} {n:>5}')
        print('\n  which rule fired — after:')
        for why, n in after.most_common():
            print(f'      {why:<28} {n:>5}')

        if gained:
            print(f'\n  a sample of the {len(gained)} newly matched:')
            for sender, why, name in gained[:25]:
                print(f'      {sender[:40]:<40} {why:<24} {name[:30]}')

        lost = [(s, o, n) for s, o, n, _x in pairs
                if o != 'unmapped' and n == 'unmapped']
        if lost:
            print(f'\n  !! {len(lost)} that the old matcher found and the '
                  f'new one does not:')
            for sender, o, n in lost[:20]:
                print(f'      {sender[:40]:<40} was {o}')

        if args.csv and pairs:
            with open(args.csv, 'w', newline='', encoding='utf-8') as fh:
                w = csv.writer(fh)
                w.writerow(['sender', 'before', 'after', 'account'])
                w.writerows(pairs)
            print(f'\n  written to {args.csv}')

        print('\nREAD-ONLY. Nothing was written.')


if __name__ == '__main__':
    main()
