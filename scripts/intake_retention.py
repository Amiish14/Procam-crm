"""
Retention for classified mail — §23.

"For the initial rollout, do not permanently discard questionable
emails." So nothing is discarded here by default: this reports what is
old enough to go, and only removes it when told.

What is kept forever
    anything that produced a lead, or attached to one — that is business
    history, not intake noise
    every correction a person made — the training set is the point

What ages out
    the stored message body on parked mail, after the retention window.
    The classification row itself stays: it is small, it is the record
    that the message was seen and judged, and losing it would make the
    accuracy figures lie about the past.

So a purge trims payloads, it does not delete decisions.

Usage
    python scripts/intake_retention.py                    # report only
    python scripts/intake_retention.py --days 90 --apply
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, '.env'))
except ImportError:
    pass

#: Classes whose bodies are worth keeping only for a while.
PERISHABLE = ('C_internal', 'I_non_business', 'D_duplicate')

DEFAULT_DAYS = 90


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=DEFAULT_DAYS,
                    help=f'retention window (default {DEFAULT_DAYS})')
    ap.add_argument('--apply', action='store_true',
                    help='actually trim the payloads')
    args = ap.parse_args()

    from app import app as flask_app, db, EmailClassification

    cutoff = datetime.utcnow() - timedelta(days=args.days)
    print(f'retention window: {args.days} days (before {cutoff:%Y-%m-%d})')

    with flask_app.app_context():
        total = EmailClassification.query.count()
        old = (EmailClassification.query
               .filter(EmailClassification.created_at < cutoff,
                       EmailClassification.classification.in_(PERISHABLE),
                       EmailClassification.created_lead_id.is_(None),
                       EmailClassification.corrected_at.is_(None))
               .all())
        with_body = [r for r in old if (r.payload or {}).get('body')]

        print(f'  classifications held:            {total:>7,}')
        print(f'  parked, past the window:         {len(old):>7,}')
        print(f'  of those still holding a body:   {len(with_body):>7,}')
        print('\n  kept regardless:')
        for label, n in (
            ('produced a lead',
             EmailClassification.query.filter(
                 EmailClassification.created_lead_id.isnot(None)).count()),
            ('corrected by a person',
             EmailClassification.query.filter(
                 EmailClassification.corrected_at.isnot(None)).count()),
        ):
            print(f'    {n:>7,}  {label}')

        if not args.apply:
            print('\n  Nothing changed. Re-run with --apply to trim the '
                  'bodies.')
            print('  The decisions themselves are never deleted — losing '
                  'them would make\n  the accuracy figures lie about the '
                  'past.')
            return

        for row in with_body:
            payload = dict(row.payload or {})
            payload['body'] = ''
            payload['body_purged_on'] = str(datetime.utcnow())[:10]
            row.payload = payload
        db.session.commit()
        print(f'\n  trimmed {len(with_body)} message body/bodies. '
              f'Every decision row is intact.')


if __name__ == '__main__':
    main()
