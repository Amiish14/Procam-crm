"""
v2026-08 — Purge test-mailbox leads before the leads@procamgroup.in cutover.

Handles four match criteria:

    --forwarded-by <email>   match leads whose notes contain
                             '[Forwarded to CRM by <email>'.
    --mailbox                match every lead created via mailbox-mode
                             ingestion (i.e. has email_message_id).
    --today                  restrict matches to leads created today (UTC).
    --ids 12,13,14           match a specific list of Lead ids.

Combine as needed. Always previews first; needs --apply to delete.

Examples:
    # See every mailbox-mode lead created today
    python scripts/2026_08_27_purge_amiish_test_leads.py --mailbox --today

    # Same, then delete
    python scripts/2026_08_27_purge_amiish_test_leads.py --mailbox --today --apply

    # Delete specific IDs
    python scripts/2026_08_27_purge_amiish_test_leads.py --ids 9919,9920,9921 --apply
"""
import argparse
import sys, os
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import app, db, Lead, LeadAttachment, EmailEvent


def build_query(args):
    q = Lead.query.filter(Lead.source == 'email')
    if args.forwarded_by:
        needle = f'[Forwarded to CRM by {args.forwarded_by}'
        q = q.filter(Lead.notes.ilike(f'%{needle}%'))
    if args.mailbox:
        q = q.filter(Lead.email_message_id.isnot(None))
    if args.today:
        # UTC midnight
        cutoff = datetime.combine(date.today(), datetime.min.time())
        q = q.filter(Lead.created_at >= cutoff)
    if args.ids:
        try:
            ids = [int(x) for x in args.ids.split(',') if x.strip()]
        except ValueError:
            print('--ids expects comma-separated integers'); sys.exit(2)
        q = q.filter(Lead.id.in_(ids))
    return q


def main(args):
    with app.app_context():
        q = build_query(args)
        rows = q.order_by(Lead.id.desc()).all()
        print(f'Leads that would be deleted: {len(rows)}')
        print(f'{"ID":>7}  {"COMPANY":30s}  {"PIC":22s}  EMAIL')
        for l in rows:
            print(f'{l.id:>7}  {(l.company or "-")[:30]:30s}  '
                  f'{(l.pic or "-")[:22]:22s}  {l.email or "-"}')
        if not rows:
            print('Nothing matched.')
            return

        if not args.apply:
            print('\nDRY RUN — re-run with --apply to actually delete.')
            return

        lead_ids = [l.id for l in rows]

        att_deleted = LeadAttachment.query.filter(
            LeadAttachment.lead_id.in_(lead_ids)).delete(synchronize_session=False)
        evt_updated = EmailEvent.query.filter(
            EmailEvent.lead_id.in_(lead_ids)).update(
            {EmailEvent.lead_id: None, EmailEvent.status: 'lead_deleted'},
            synchronize_session=False)
        lead_deleted = Lead.query.filter(Lead.id.in_(lead_ids))\
                                  .delete(synchronize_session=False)
        db.session.commit()
        print(f'\n  [OK] Deleted {lead_deleted} leads · '
              f'{att_deleted} attachments · '
              f'{evt_updated} email events unlinked (kept as history).')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--forwarded-by',
                   help='Only leads forwarded by this internal email address.')
    p.add_argument('--mailbox', action='store_true',
                   help='Only leads created via mailbox-mode (email_message_id set).')
    p.add_argument('--today', action='store_true',
                   help='Only leads created today (UTC).')
    p.add_argument('--ids',
                   help='Comma-separated Lead IDs to target explicitly.')
    p.add_argument('--apply', action='store_true',
                   help='Actually delete. Without this, prints preview only.')
    main(p.parse_args())
