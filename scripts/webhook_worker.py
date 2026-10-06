"""
Deliver the CRM events the TMS is owed.

Run from a systemd timer every two minutes. One pass claims the
events that are due, posts each to the TMS, and records what
happened. `leases.hold` means two overlapping runs cannot both claim
the same event.

    .venv/bin/python scripts/webhook_worker.py --status
    .venv/bin/python scripts/webhook_worker.py --dry-run
    .venv/bin/python scripts/webhook_worker.py

Exit 0 when everything due was delivered or is legitimately waiting,
1 when something went dead and wants a person.
"""
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import app                                       # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=25)
    ap.add_argument('--status', action='store_true')
    ap.add_argument('--dry-run', dest='dry_run', action='store_true')
    ap.add_argument('--json', dest='as_json', action='store_true')
    args = ap.parse_args()

    with app.app_context():
        from app.services import leases, webhooks

        if args.status:
            report = {'webhooks': webhooks.health(),
                      'leases': leases.status(),
                      'destination_configured':
                          bool((os.environ.get('TMS_WEBHOOK_URL') or '').strip()),
                      'token_configured':
                          bool(os.environ.get('TMS_WEBHOOK_TOKEN'))}
            if args.as_json:
                print(json.dumps(report, indent=2, default=str))
            else:
                health = report['webhooks']
                print(f"waiting {health['waiting']}  dead {health['dead']}  "
                      f"oldest waiting {health['oldest_waiting_minutes']} min")
                for key, count in sorted(health['counts'].items()):
                    print(f'  {key:12} {count}')
                print(f"  destination configured: "
                      f"{report['destination_configured']}")
                print(f"  token configured:       "
                      f"{report['token_configured']}")
                for row in health['dead_rows'][:10]:
                    print(f"  DEAD {row['event_id']}  {row['last_error']}")
            return 0

        if not (os.environ.get('TMS_WEBHOOK_URL') or '').strip():
            print('TMS_WEBHOOK_URL is not set — events are being queued '
                  'and nothing can be delivered. They are not lost; they '
                  'will go once it is configured.')

        if args.dry_run:
            from datetime import datetime

            from app.models.integration import WebhookOutbox
            due = (WebhookOutbox.query
                   .filter(WebhookOutbox.status.in_(('queued', 'failed')),
                           WebhookOutbox.next_attempt_at
                           <= datetime.utcnow())
                   .order_by(WebhookOutbox.next_attempt_at)
                   .limit(args.limit).all())
            print(f'{len(due)} event(s) due now:')
            for row in due:
                print(f"  {row.event_id}  attempt "
                      f"{(row.attempts or 0) + 1}  {row.event_type}")
            print('\n== DRY RUN — nothing sent ==')
            return 0

        report = webhooks.run_once(limit=args.limit)
        print(f"claimed={report['claimed']} delivered={report['delivered']} "
              f"failed={report['failed']} dead={report['dead']} "
              f"unstuck={report['unstuck']} skipped={report['skipped']}")
        return 1 if report['dead'] else 0


if __name__ == '__main__':
    sys.exit(main())
