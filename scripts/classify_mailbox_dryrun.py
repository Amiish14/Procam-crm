"""
Run the intake classifier over real mail without creating anything.

The classifier is tested hard against fixtures and has never seen the
actual mailbox. This reads recent messages from leads@procamgroup.in,
classifies each one, and prints what it would have done — no lead, no
row, no change of any kind.

It answers the question the design could only estimate: what share of
the intake is genuinely a new enquiry, and what share is replies,
forwards, quotations and rate requests. The thresholds in
lead_intake.py should be calibrated against this output rather than
against my guesses.

Usage
    python scripts/classify_mailbox_dryrun.py                 # last 7 days
    python scripts/classify_mailbox_dryrun.py --days 30
    python scripts/classify_mailbox_dryrun.py --days 30 --show A_new_lead
    python scripts/classify_mailbox_dryrun.py --days 30 --csv /tmp/sample.csv

The CSV is the labelled seed set §25 asks for: one row per message with
what the classifier decided, ready for a human to correct.
"""
import argparse
import csv
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, '.env'))
except ImportError:
    pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=7)
    ap.add_argument('--limit', type=int, default=500)
    ap.add_argument('--show', help='print every message in this class')
    ap.add_argument('--csv', help='write the labelled sample here')
    args = ap.parse_args()

    from app import app as flask_app                          # noqa: E402
    from app.services import lead_intake as li                # noqa: E402
    from app.services import lead_intake_db as lidb           # noqa: E402
    from email_ingest.graph_client import GraphClient         # noqa: E402
    from email_ingest import service as mail_service          # noqa: E402

    mailbox = mail_service.crm_inbox_email() \
        or os.environ.get('EMAIL_INGEST_MAILBOX')
    if not mailbox:
        raise SystemExit('No mailbox configured.')

    print(f'mailbox: {mailbox}')
    print(f'window:  last {args.days} day(s)')
    print('NOTHING WILL BE CREATED OR CHANGED.\n')

    client = GraphClient()
    since = datetime.now(timezone.utc) - timedelta(days=args.days)

    with flask_app.app_context():
        ctx = lidb.build_context()
        counts = Counter()
        steps = Counter()
        rows = []
        scanned = 0

        for msg in client.list_messages(mailbox, since_utc=since, top=100):
            scanned += 1
            if scanned > args.limit:
                break
            try:
                d = li.classify(msg, ctx)
            except Exception as exc:
                counts['ERROR'] += 1
                print(f'  !! {msg.get("subject", "")[:50]}: {exc}')
                continue

            counts[d.klass] += 1
            steps[f'step_{d.step}'] += 1
            row = {
                'received': (msg.get('receivedDateTime') or '')[:19],
                'from': li.sender(msg),
                'subject': (msg.get('subject') or '')[:120],
                'classification': d.klass,
                'label': d.label,
                'decided_by': f'step_{d.step}',
                'confidence': d.confidence if d.confidence is not None else '',
                'duplicate_score': d.duplicate_score,
                'matched_lead': d.lead_id or '',
                'reason': d.reason,
                'corrected_to': '',      # for a human to fill in
            }
            rows.append(row)
            if args.show and d.klass == args.show:
                print(f'  {row["received"]}  {row["from"][:34]:<36}'
                      f'{row["subject"][:52]}')

        if not scanned:
            print('  No messages in the window. Widen --days.')
            return

        print(f'\n{"=" * 62}')
        print(f'  {scanned} message(s) classified')
        print('=' * 62)
        creates = counts.get(li.Klass.NEW_LEAD, 0)
        for klass, n in counts.most_common():
            label = li.Klass.LABELS.get(klass, klass)
            mark = '→ LEAD' if klass == li.Klass.NEW_LEAD else ''
            print(f'  {n:>5}  {pct(n, scanned):>5}  {label:<32}{mark}')

        print(f'\n  which rule decided:')
        for step, n in steps.most_common():
            print(f'  {n:>5}  {pct(n, scanned):>5}  {step}')

        noise = scanned - creates
        print(f'\n  Would create {creates} lead(s) from {scanned} message(s).')
        print(f'  Noise reduction: {pct(noise, scanned)} '
              f'({noise} message(s) would not create a lead).')
        review = counts.get(li.Klass.REVIEW, 0)
        if review:
            print(f'  {review} would go to Admin Review rather than being '
                  f'decided automatically.')

    if args.csv:
        with open(args.csv, 'w', newline='', encoding='utf-8') as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f'\n  Labelled sample: {args.csv}')
        print('  Fill in corrected_to where the classifier is wrong — that '
              'column is\n  the training set, and the disagreements are the '
              'valuable part.')

    print('\n  Nothing was created or changed.')


def pct(n, total):
    return f'{(100.0 * n / total):.0f}%' if total else '0%'


if __name__ == '__main__':
    main()
