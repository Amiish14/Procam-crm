#!/usr/bin/env python
"""
v2026-09-02 — Restore leads deleted by an over-broad purge.

On 2026-09-02 the purge script used the parser's bulk-sender regex, which
matched no-reply@ / noreply@ and so deleted ~100 genuine leads from
procurement portals — SuperProcure load tenders, SAP Ariba "new business
lead", JustDial customer enquiries. The purge now defers to
email_ingest.blocklist, the same rule ingestion uses.

The emails themselves were never touched; only the Lead rows were removed,
and each carries an EmailEvent marked 'lead_deleted' which stops re-ingest.
This walks those events, re-fetches each message, and re-checks the sender
against the CORRECTED blocklist:

    still blocked  -> genuinely junk, mark left in place
    now allowed    -> wrongly deleted, mark cleared and the lead rebuilt

Safe to re-run, and safe to run after adding domains to the blocklist —
anything genuinely junk simply stays deleted.

Examples:
    python scripts/2026_09_02_restore_wrongly_purged.py            # preview
    python scripts/2026_09_02_restore_wrongly_purged.py --apply
"""
import argparse
import os
import sys
from collections import Counter

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
from email_ingest import blocklist                           # noqa: E402
from email_ingest.graph_client import GraphClient            # noqa: E402
from email_ingest.single_message import process_single_message  # noqa: E402
from email_ingest.webhook import _get_message                # noqa: E402


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true', help='restore them')
    ap.add_argument('--limit', type=int)
    args = ap.parse_args()

    mailbox = mail_service.crm_inbox_email()
    graph = GraphClient()
    out = Counter()

    with app.app_context():
        q = (EmailEvent.query
             .filter(EmailEvent.status == 'lead_deleted',
                     EmailEvent.internet_message_id.isnot(None))
             .order_by(EmailEvent.received_at))
        events = q.limit(args.limit).all() if args.limit else q.all()

        print(f'mailbox            : {mailbox}')
        print(f'deleted events     : {len(events)}')
        print(f'blocklist domains  : {len(blocklist.blocked_domains())}')
        if not args.apply:
            print('\nPREVIEW — nothing will be written.\n')

        for evt in events:
            try:
                msg = _get_message(graph, mailbox, evt.internet_message_id)
            except Exception as e:                               # noqa: BLE001
                out['message no longer in mailbox'] += 1
                continue

            extracted = email_parser.extract_lead(msg)
            sender = ((extracted or {}).get('email') or '').strip().lower()
            reason = blocklist.check(sender)
            if reason:
                out['still junk — left deleted'] += 1
                continue

            subj = (evt.subject or '')[:52]
            if not args.apply:
                out['would restore'] += 1
                print('%-19s %-34s %s' % (str(evt.received_at)[:19],
                                          sender[:34], subj))
                continue

            # Clear the tombstone so process_single_message will rebuild it.
            evt.status = 'restored'
            evt.reason = 'wrongly purged 2026-09-02; blocklist corrected'
            db.session.commit()

            result = process_single_message(graph, mailbox=mailbox, msg=msg)
            if result['status'] == 'created':
                evt.status = 'lead_created'
                evt.lead_id = result.get('lead_id')
                out['restored'] += 1
                print('%-19s lead=%-6s %-32s %s' % (
                    str(evt.received_at)[:19], result.get('lead_id'),
                    sender[:32], subj))
            else:
                evt.status = 'skipped'
                evt.reason = (result.get('reason') or '')[:300]
                out[f"not recreated: {result.get('reason', '?')[:40]}"] += 1
            db.session.commit()

    print('\n--- summary ---')
    for k, n in out.most_common():
        print(f'  {n:>5}  {k}')
    if not args.apply:
        print('\nPreview only. Add --apply to restore.')


if __name__ == '__main__':
    main()
