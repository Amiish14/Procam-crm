#!/usr/bin/env python
"""
v2026-09-02 — Replay email notifications that were wrongly rejected.

Between the mailbox-lockdown deploy and 2026-09-02, every Graph
notification was rejected as 'wrong mailbox': the check matched the
mailbox UPN as a substring of the notification `resource`, but Graph names
the mailbox there by its directory object id. No leads were ingested at
all in that window.

Those EmailEvent rows cannot go through /api/email/inbox/<id>/retry —
that endpoint needs `internet_message_id`, which is only populated after
a successful fetch, so it is NULL on every rejected row. This script
instead pulls the Graph message id straight out of the stored
`payload_json` (resourceData.id), fetches the message from the sanctioned
leads mailbox, and runs the normal ingest path over it.

Safe to re-run: process_single_message() is idempotent, so a message that
already produced a Lead comes back 'already ingested'.

Examples:
    # See what would be replayed
    python scripts/2026_09_02_replay_rejected_events.py

    # Actually create the leads
    python scripts/2026_09_02_replay_rejected_events.py --apply

    # Only events since a date
    python scripts/2026_09_02_replay_rejected_events.py --apply --since 2026-09-01
"""
import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db, EmailEvent                     # noqa: E402
from email_ingest import service as mail_service        # noqa: E402
from email_ingest.graph_client import GraphClient       # noqa: E402
from email_ingest.single_message import process_single_message  # noqa: E402
from email_ingest.webhook import _get_message           # noqa: E402


def message_id_of(evt) -> str:
    """Graph message id for an event: the stored internetMessageId if we
    have one, else resourceData.id out of the saved notification."""
    if evt.internet_message_id:
        return evt.internet_message_id
    try:
        payload = json.loads(evt.payload_json or '{}')
    except Exception:
        return ''
    return ((payload.get('resourceData') or {}).get('id') or '').strip()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true',
                    help='actually ingest (default: preview only)')
    ap.add_argument('--status', default='rejected',
                    help="event status to replay (default: rejected; "
                         "'all' for every status)")
    ap.add_argument('--since', metavar='YYYY-MM-DD',
                    help='only events received on or after this date')
    ap.add_argument('--limit', type=int, help='stop after N events')
    args = ap.parse_args()

    mailbox = mail_service.crm_inbox_email()

    with app.app_context():
        q = EmailEvent.query
        if args.status != 'all':
            q = q.filter(EmailEvent.status == args.status)
        if args.since:
            try:
                cutoff = datetime.strptime(args.since, '%Y-%m-%d')
            except ValueError:
                sys.exit(f'--since must be YYYY-MM-DD, got {args.since!r}')
            q = q.filter(EmailEvent.received_at >= cutoff)
        q = q.order_by(EmailEvent.received_at)
        events = q.limit(args.limit).all() if args.limit else q.all()

        print(f'mailbox : {mailbox}')
        print(f'events  : {len(events)} with status={args.status}'
              f'{" since " + args.since if args.since else ""}')
        if not events:
            return
        if not args.apply:
            print('\nPREVIEW — nothing will be written. Re-run with --apply.\n')

        graph = GraphClient()
        outcomes = Counter()

        for evt in events:
            msg_ref = message_id_of(evt)
            when = str(evt.received_at)[:19]
            if not msg_ref:
                outcomes['no message id in payload'] += 1
                print(f'{when}  #{evt.id:<6} SKIP  no message id in payload')
                continue

            try:
                msg = _get_message(graph, mailbox, msg_ref)
            except Exception as e:                        # noqa: BLE001
                # A 404 here means the message genuinely is not in the leads
                # mailbox (deleted, moved out of Inbox, or truly another
                # mailbox). Nothing to replay.
                outcomes['fetch failed'] += 1
                print(f'{when}  #{evt.id:<6} FETCH-FAIL  {str(e)[:110]}')
                continue

            subject = (msg.get('subject') or '')[:60]
            if not args.apply:
                frm = (((msg.get('from') or {}).get('emailAddress') or {})
                       .get('address') or '-')
                outcomes['would ingest'] += 1
                print(f'{when}  #{evt.id:<6} WOULD-INGEST  {frm:<38} {subject}')
                continue

            result = process_single_message(graph, mailbox=mailbox, msg=msg)
            status = result['status']
            outcomes[status] += 1
            evt.internet_message_id = (msg.get('internetMessageId')
                                       or msg.get('id'))
            evt.subject = (msg.get('subject') or '')[:250]
            if status == 'created':
                evt.status, evt.lead_id, evt.reason = ('lead_created',
                                                       result.get('lead_id'),
                                                       'replayed 2026-09-02')
            else:
                evt.status = status
                evt.reason = (result.get('reason') or '')[:300]
            db.session.commit()
            lead = result.get('lead_id') or '-'
            print(f'{when}  #{evt.id:<6} {status.upper():<12} '
                  f'lead={lead:<6} {subject}')

        print('\n--- summary ---')
        for k, n in outcomes.most_common():
            print(f'  {n:>5}  {k}')
        if not args.apply:
            print('\nPreview only. Re-run with --apply to ingest these.')


if __name__ == '__main__':
    main()
