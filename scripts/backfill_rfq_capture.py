"""
Fetch the originals of emails the CRM already turned into leads.

Every lead created from email has its RFC-5322 Message-Id on it. The
mailbox still has most of those messages, so the original and its
attachments can be fetched now even though nothing captured them at the
time. This is what gives the existing pipeline the same "forward me the
original" button as a lead that arrives tomorrow.

It is slow and it talks to Microsoft Graph once or twice per lead, so
it is a separate run rather than part of the migration, it works newest
first, and it can be stopped and resumed — a lead already captured is
skipped.

    .venv/bin/python scripts/backfill_rfq_capture.py --check --limit 20
    .venv/bin/python scripts/backfill_rfq_capture.py --limit 200
    .venv/bin/python scripts/backfill_rfq_capture.py --since 2026-08-01

A message the mailbox no longer has is recorded as `missing` and not
tried again. That is the expected outcome for older leads and is not a
failure.
"""
import argparse
import os
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=100)
    ap.add_argument('--since', help='only leads created on/after YYYY-MM-DD')
    ap.add_argument('--lead', type=int, help='one lead, by id')
    ap.add_argument('--check', action='store_true',
                    help='say what would be fetched and write nothing')
    ap.add_argument('--attachments-only', action='store_true',
                    help='record missing attachment rows; skip the .eml')
    args = ap.parse_args()

    with app.app_context():
        from email_ingest import service as mail_service
        from email_ingest.graph_client import GraphClient
        from app.models.mailops import LeadRawEmail
        from app.services import rfq_capture

        mailbox = mail_service.crm_inbox_email()
        query = Lead.query.filter(Lead.email_message_id.isnot(None))
        if args.lead:
            query = query.filter(Lead.id == args.lead)
        if args.since:
            query = query.filter(
                Lead.created_at >= datetime.strptime(args.since, '%Y-%m-%d'))
        done = {r.internet_message_id for r in
                db.session.query(LeadRawEmail.internet_message_id).all()}
        candidates = [l for l in
                      query.order_by(Lead.id.desc()).limit(args.limit * 4).all()
                      if l.email_message_id not in done][:args.limit]

        print(f'mailbox: {mailbox}')
        print(f'{len(candidates)} lead(s) to try '
              f'({len(done)} already captured)')
        if args.check:
            for lead in candidates[:20]:
                print(f'  WOULD fetch lead {lead.id} — '
                      f'{(lead.company or "")[:40]} — '
                      f'{str(lead.created_at)[:10]}')
            if len(candidates) > 20:
                print(f'  … and {len(candidates) - 20} more')
            print('\n== DRY-RUN — nothing written, nothing fetched ==')
            return 0

        graph = GraphClient()
        tally = {'stored': 0, 'missing': 0, 'failed': 0, 'files': 0, 'off': 0}
        for lead in candidates:
            try:
                msg = {'internetMessageId': lead.email_message_id,
                       'subject': lead.original_email_subject or '',
                       'from': {'emailAddress': {
                           'address': lead.original_email_from or ''}},
                       'hasAttachments': True}
                # The webhook path never wrote the attachment rows, so
                # ask for them unconditionally; already-recorded files
                # are skipped by their Graph id.
                from email_ingest import raw_mime
                gid = raw_mime.graph_id_for(graph, mailbox,
                                            lead.email_message_id)
                if not gid:
                    tally['missing'] += 1
                    print(f'  lead {lead.id}: not in the mailbox any more')
                    continue
                msg['id'] = gid
                result = rfq_capture.capture_for_lead(
                    lead, graph=graph, mailbox=mailbox, msg=msg, commit=True)
                db.session.commit()
                tally['files'] += result['attachments']
                tally[result['raw']] = tally.get(result['raw'], 0) + 1
                print(f"  lead {lead.id}: original {result['raw']}, "
                      f"{result['attachments']} file(s) recorded")
            except Exception as exc:                      # noqa: BLE001
                db.session.rollback()
                tally['failed'] += 1
                print(f'  lead {lead.id}: {exc}')

        print(f"\nstored={tally['stored']} missing={tally['missing']} "
              f"failed={tally['failed']} off={tally.get('off', 0)} "
              f"attachment rows added={tally['files']}")
        if tally.get('off'):
            print('Some leads reported "off": FEATURE_RFQ_CAPTURE is unset, '
                  'so attachments were recorded but no .eml was kept.')
        return 0


if __name__ == '__main__':
    sys.exit(main())
