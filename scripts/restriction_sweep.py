"""
The daily pass over the client block register.

Two jobs, both on a timer because neither has a moment that triggers
it:

  * **review dates.** Somebody blocking a client often says "look at
    this again in six months". This is what remembers. Sent once —
    `review_reminded_at` is stamped, so a date that has passed does not
    produce a reminder every morning until somebody acts.
  * **the attempts digest.** Everyone who tried to create business with
    a restricted client since the last run, in one email to the
    administrators. The individual refusal is already logged against
    the register; this is the pattern, which is the thing worth
    reading — one person trying once is a mistake, the same person
    three times is a conversation.

    .venv/bin/python scripts/restriction_sweep.py --dry-run
    .venv/bin/python scripts/restriction_sweep.py
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import app, db                                   # noqa: E402

LEASE = 'restriction_sweep'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--hours', type=int, default=24,
                    help='how far back the attempts digest looks')
    args = ap.parse_args()

    with app.app_context():
        from app.models.restriction import (BLOCKED, CAUTION,
                                            ClientRestriction,
                                            ClientRestrictionEvent)
        from app.services import (leases, restriction_notify as tell,
                                  restriction_register as register,
                                  sales_rules as rules)

        today = rules.business_today()
        due = (ClientRestriction.query
               .filter(ClientRestriction.status.in_((BLOCKED, CAUTION)),
                       ClientRestriction.review_date.isnot(None),
                       ClientRestriction.review_date <= today,
                       ClientRestriction.review_reminded_at.is_(None))
               .all())

        since = datetime.utcnow() - timedelta(hours=args.hours)
        attempts = (ClientRestrictionEvent.query
                    .filter(ClientRestrictionEvent.action == 'attempt_blocked',
                            ClientRestrictionEvent.created_at >= since)
                    .order_by(ClientRestrictionEvent.created_at).all())
        admins = register.approvers()

        print(f'reviews due: {len(due)}')
        print(f'attempts in the last {args.hours}h: {len(attempts)}')
        print(f'administrators to tell: {len(admins)}')
        if args.dry_run:
            for row in due:
                print(f'  WOULD remind about {row.company_name} '
                      f'(review {row.review_date})')
            for event in attempts[:20]:
                print(f'  {event.user_id or "?"} — '
                      f'{event.payload.get("what", "?")} '
                      f'at {str(event.created_at)[:16]}')
            print('\n== DRY-RUN — nothing sent ==')
            return 0

        with leases.hold(LEASE, seconds=600) as got:
            if not got:
                print('another sweep holds the lease — skipping.')
                return 0
            reminded = 0
            for row in due:
                try:
                    tell.review_due(row)
                    row.review_reminded_at = datetime.utcnow()
                    reminded += 1
                except Exception:                         # noqa: BLE001
                    app.logger.exception('review reminder failed for %s',
                                         row.id)
            db.session.commit()

            sent = 0
            if attempts and admins:
                sent = tell.daily_attempt_digest(attempts, admins)

        print(f'reminded={reminded} digest_sent_to={sent}')
        return 0


if __name__ == '__main__':
    sys.exit(main())
