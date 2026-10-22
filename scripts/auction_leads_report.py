"""
CRM-FORWARD-01 item 3 — what an auction rule would have caught.

    .venv/bin/python scripts/auction_leads_report.py --check
    .venv/bin/python scripts/auction_leads_report.py --seed

READ ONLY by default. `--seed` writes the patterns into the
Master Data list `auction_pattern`; it changes no lead.

Classification runs at ingest, so this says what the last 90 days
would have looked like had it been running — by platform and by
customer, and how many were deleted or swept.
"""
import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402
from app.services import lead_kinds                       # noqa: E402

DAYS = 90


def _subject(lead):
    """What the subject was, including the Fw: the parser strips.

    `original_email_subject` holds the stripped original and is
    empty on a lot of forwards, so the ingest event is the only
    place the subject as received survives.
    """
    for value in (lead.forwarded_subject,
                  lead.original_email_subject):
        if (value or '').strip():
            return value
    row = db.session.execute(db.text(
        'SELECT subject FROM email_events '
        'WHERE internet_message_id = :m ORDER BY id DESC LIMIT 1'),
        {'m': lead.email_message_id or ''}).fetchone()
    return (row[0] if row else '') or ''


def survey(days=DAYS):
    since = datetime.utcnow() - timedelta(days=days)
    leads = (Lead.query
             .filter(Lead.source == 'email',
                     Lead.created_at >= since)
             .order_by(Lead.created_at.asc()).all())
    rules = lead_kinds.patterns()
    hit = []
    for lead in leads:
        kind, platform, why = lead_kinds.classify(
            subject=_subject(lead),
            sender=(lead.original_email_from or lead.email or ''),
            rules=rules)
        if kind:
            hit.append((lead, platform or '(unknown)', why))
    return leads, hit


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true', default=True)
    ap.add_argument('--seed', action='store_true')
    ap.add_argument('--days', type=int, default=DAYS)
    ap.add_argument('--actor', default='system')
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        print(f'MODE:     {"SEED" if args.seed else "READ ONLY"}\n')

        leads, hit = survey(args.days)
        print(f'  email leads in {args.days} days   {len(leads)}')
        print(f'  would be "auction / spot load"  {len(hit)}')

        def show(title, counter, limit=10):
            print(f'\n  {title}')
            for key, n in counter.most_common(limit):
                print(f'    {n:>4}  {key}')

        show('by platform', Counter(p for _l, p, _w in hit))
        show('by customer',
             Counter((l.company or '(none)') for l, _p, _w in hit))
        show('by month',
             Counter(str(l.created_at)[:7] for l, _p, _w in hit))
        show('matched because', Counter(w for _l, _p, w in hit), 8)
        show('where they are now',
             Counter((l.stage or '') for l, _p, _w in hit), 6)

        forwarded = [l for l, _p, _w in hit
                     if (l.forwarded_by_email or '').strip()]
        print(f'\n  of those, a forwarder is recorded: '
              f'{len(forwarded)}')
        print('    (nothing is backfilled — the forwarder was not '
              'stored before this release, and no raw .eml '
              'survives to recover it from)')

        if not args.seed:
            print('\nREAD ONLY — nothing written. `--seed` puts the '
                  'patterns in Master Data; classification then '
                  'runs on new mail at ingest.')
            return

        made = lead_kinds.seed(actor=args.actor)
        db.session.commit()
        print(f'\n  {len(made)} pattern(s) written to Master Data '
              f'list "{lead_kinds.LIST_KEY}".')
        print(f'  Set the owner in admin_settings under '
              f'"{lead_kinds.OWNER_SETTING}" — no name is in code.')


if __name__ == '__main__':
    main()
