"""
Send what is waiting in the outbox.

Run from a systemd timer every two minutes. One pass claims the rows
that are due, groups the ones meant to arrive together, sends them and
records what happened. Holding `leases.hold('email_outbox')` means two
overlapping runs cannot both claim the same row.

    .venv/bin/python scripts/outbox_worker.py --dry-run
    .venv/bin/python scripts/outbox_worker.py
    .venv/bin/python scripts/outbox_worker.py --status

Exit codes: 0 nothing to do or everything sent, 1 something failed
(systemd units in this repository set SuccessExitStatus=1 so a failed
send does not mark the timer broken), 2 could not run at all.
"""
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import app                                       # noqa: E402
from app.services import flags, leases, outbox            # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=outbox.CLAIM_LIMIT)
    ap.add_argument('--dry-run', action='store_true',
                    help='claim nothing; show what is due')
    ap.add_argument('--status', action='store_true',
                    help='queue health and the leases')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()

    with app.app_context():
        if args.status:
            report = {'health': outbox.health(), 'leases': leases.status(),
                      'flags': dict((n, s) for n, _d, s in flags.all_flags())}
            if args.json:
                print(json.dumps(report, indent=2, default=str))
            else:
                h = report['health']
                print(f"queued {h['queued']}  failed {h['failed']}  "
                      f"oldest queued {h['oldest_queued_minutes']} min")
                for key, count in sorted(h['totals'].items()):
                    print(f'  {key:10} {count}')
                for lease in report['leases']:
                    print(f"  lease {lease['name']}: "
                          f"{'held by ' + lease['holder'] if lease['held'] else 'free'}")
            return 0

        if not flags.on('FEATURE_EMAIL_NOTIFY'):
            print('FEATURE_EMAIL_NOTIFY is off — nothing is being queued, '
                  'so there is nothing for this worker to send. It will '
                  'still drain anything already in the queue.')

        if args.dry_run:
            from datetime import datetime
            from app.models.mailops import EmailOutbox
            due = (EmailOutbox.query
                   .filter(EmailOutbox.status == 'queued',
                           EmailOutbox.not_before <= datetime.utcnow())
                   .order_by(EmailOutbox.not_before).limit(args.limit).all())
            print(f'{len(due)} due now:')
            for row in due:
                print(f'  #{row.id} {row.to_addr} — {row.subject[:70]}')
            return 0

        report = outbox.run_once(limit=args.limit)
        print(f"claimed={report['claimed']} groups={report['groups']} "
              f"sent={report['sent']} failed={report['failed']} "
              f"unstuck={report['unstuck']} skipped={report['skipped']}")
        return 1 if report['failed'] else 0


if __name__ == '__main__':
    sys.exit(main())
