#!/usr/bin/env python
"""
v2026-09-02 — Date email leads by when the mail ARRIVED, not when it was
processed.

Lead.created_at used to be stamped with datetime.utcnow() at ingest time.
The portal sorts on that column, so any backfill or replay bunched old mail
at the top of the list under today's timestamp. Ingestion now uses the
message's Graph receivedDateTime; this script repairs the existing rows.

Source of truth, in order:
  1. Graph receivedDateTime  — exact, needs --graph (one API call per lead)
  2. EmailEvent.received_at  — when the webhook was notified, within seconds
                               of arrival. Default: fast and no API calls.

Leads ingested before the leads@ cutover have no EmailEvent and cannot be
fetched from the leads mailbox, so they are left alone.

Examples:
    python scripts/2026_09_02_backfill_lead_dates.py            # preview
    python scripts/2026_09_02_backfill_lead_dates.py --apply
    python scripts/2026_09_02_backfill_lead_dates.py --apply --graph
"""
import argparse
import os
import sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db, Lead, EmailEvent                   # noqa: E402
from email_ingest import service as mail_service            # noqa: E402
from email_ingest import parser as email_parser             # noqa: E402

# Only shift a date if it is out by more than this — avoids churning rows
# that are already effectively right.
_TOLERANCE_SECONDS = 120


def event_times() -> dict:
    """lead_id -> earliest notification time (closest to actual arrival)."""
    out = {}
    rows = (db.session.query(EmailEvent.lead_id, EmailEvent.received_at)
            .filter(EmailEvent.lead_id.isnot(None),
                    EmailEvent.received_at.isnot(None)).all())
    for lead_id, received in rows:
        if lead_id not in out or received < out[lead_id]:
            out[lead_id] = received
    return out


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true', help='write the changes')
    ap.add_argument('--graph', action='store_true',
                    help='fetch exact receivedDateTime from Graph (slower)')
    ap.add_argument('--limit', type=int)
    args = ap.parse_args()

    with app.app_context():
        times = event_times()
        leads = (Lead.query.filter(Lead.source == 'email',
                                   Lead.id.in_(times.keys()))
                 .order_by(Lead.id).all()) if times else []
        if args.limit:
            leads = leads[:args.limit]

        print('leads with a mailbox event :', len(leads))
        if not leads:
            print('Nothing to backfill.')
            return
        if not args.apply:
            print('\nPREVIEW — nothing will be written.\n')

        graph = None
        if args.graph:
            from email_ingest.graph_client import GraphClient
            graph = GraphClient()
        mailbox = mail_service.crm_inbox_email()

        out = Counter()
        print('%-7s %-21s %-21s %s' % ('id', 'created_at (now)',
                                       'arrived (correct)', 'shift'))
        print('-' * 78)
        for lead in leads:
            arrived = times.get(lead.id)
            if graph is not None and lead.email_message_id:
                try:
                    from email_ingest.webhook import _get_message
                    msg = _get_message(graph, mailbox, lead.email_message_id)
                    exact = email_parser.received_datetime(msg)
                    if exact:
                        arrived = exact
                except Exception:
                    out['graph fetch failed (used event time)'] += 1
            if not arrived:
                out['no arrival time'] += 1
                continue

            cur = lead.created_at
            delta = abs((cur - arrived).total_seconds()) if cur else None
            if delta is not None and delta <= _TOLERANCE_SECONDS:
                out['already correct'] += 1
                continue

            hrs = (delta or 0) / 3600.0
            print('%-7s %-21s %-21s %.1fh' % (
                lead.id, str(cur)[:19], str(arrived)[:19], hrs))
            if args.apply:
                lead.created_at = arrived
                lead.onboarded_date = arrived.date()
            out['fixed' if args.apply else 'would fix'] += 1

        if args.apply:
            db.session.commit()

        print('\n--- summary ---')
        for k, n in out.most_common():
            print(f'  {n:>5}  {k}')
        if not args.apply:
            print('\nPreview only. Add --apply to write the dates.')
        else:
            print('\nDone. The portal sorts on created_at, so the lead list '
                  'now reads in the order the mail actually arrived.')


if __name__ == '__main__':
    main()
