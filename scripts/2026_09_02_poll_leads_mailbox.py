#!/usr/bin/env python
"""
v2026-09-02 — Poll leads@procamgroup.in directly and ingest every message.

Standalone fallback for when the Graph webhook is unavailable (e.g. the
subscription cannot be created because notification-URL validation is
failing). It reads the mailbox over the Graph REST API — no subscription,
no notification URL, nothing to validate — and runs each message through
process_single_message(), the exact code path the webhook uses.

Identical results to webhook ingestion:
  * every message becomes a Lead (the inbox is the filter)
  * forwards are unwrapped to the original prospect
  * dedup on internetMessageId, so overlapping runs never double-insert
  * an EmailEvent row per message, so the portal's admin view and the
    re-enrich / date-backfill scripts keep working

Safe to run on a short timer: a message already ingested returns
'already ingested' and costs one cheap DB lookup.

Usage:
    python scripts/2026_09_02_poll_leads_mailbox.py                 # 6h window
    python scripts/2026_09_02_poll_leads_mailbox.py --hours 48
    python scripts/2026_09_02_poll_leads_mailbox.py --dry-run
"""
import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
except ImportError:                                              # pragma: no cover
    pass

from app import app, db, EmailEvent                          # noqa: E402
from email_ingest import service as mail_service             # noqa: E402
from email_ingest import parser as email_parser              # noqa: E402
from email_ingest.graph_client import GraphClient            # noqa: E402
from email_ingest.single_message import process_single_message  # noqa: E402


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--hours', type=float, default=6.0,
                    help='how far back to scan (default 6)')
    ap.add_argument('--dry-run', action='store_true',
                    help='list what would be ingested, write nothing')
    args = ap.parse_args()

    mailbox = mail_service.crm_inbox_email()
    since = datetime.utcnow() - timedelta(hours=args.hours)
    print(f'mailbox : {mailbox}')
    print(f'since   : {since.isoformat()}Z ({args.hours}h window)')

    graph = GraphClient()
    out = Counter()

    with app.app_context():
        for msg in graph.list_messages(mailbox=mailbox, since_utc=since,
                                       top=100):
            imid = msg.get('internetMessageId') or msg.get('id')
            subject = (msg.get('subject') or '')[:52]
            arrived = email_parser.received_datetime(msg)

            if args.dry_run:
                out['would ingest'] += 1
                print('%-19s %s' % (str(arrived)[:19], subject))
                continue

            result = process_single_message(graph, mailbox=mailbox, msg=msg)
            status = result['status']
            out[status] += 1
            if status == 'skipped' and result.get('reason') == 'already ingested':
                continue        # quiet: this is the normal case on a re-run

            # Mirror the webhook's audit trail so the admin Email Inbox page
            # and the re-enrich / backfill scripts see polled mail too.
            evt = EmailEvent(
                received_at=arrived or datetime.utcnow(),
                mailbox=mailbox,
                internet_message_id=imid,
                subject=(msg.get('subject') or '')[:250],
                status=('lead_created' if status == 'created' else status),
                reason=(result.get('reason') or 'polled')[:300],
                lead_id=result.get('lead_id'),
            )
            db.session.add(evt)
            db.session.commit()
            print('%-19s %-13s lead=%-6s %s' % (
                str(arrived)[:19], status, result.get('lead_id') or '-',
                subject))

    print('\n--- summary ---')
    for k, n in out.most_common():
        print(f'  {n:>5}  {k}')


if __name__ == '__main__':
    main()
