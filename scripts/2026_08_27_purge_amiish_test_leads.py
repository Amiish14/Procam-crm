"""
v2026-08 — Purge test-forwarded leads that came through amiish.sinha@procamgroup.in.

These were created while we were using amiish's mailbox for testing before
the switch to leads@procamgroup.in. Every one of them carries a
`[Forwarded to CRM by amiish.sinha@procamgroup.in ...]` breadcrumb in the
`notes` field, so we can target them precisely without touching any real
leads.

Usage:
    python scripts/2026_08_27_purge_amiish_test_leads.py           # dry-run
    python scripts/2026_08_27_purge_amiish_test_leads.py --apply   # delete
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app import app, db, Lead, LeadAttachment, EmailEvent

# The exact substring that identifies leads forwarded through amiish's mailbox.
NEEDLE = '[Forwarded to CRM by amiish.sinha@procamgroup.in'


def main(apply_changes: bool):
    with app.app_context():
        q = Lead.query.filter(Lead.source == 'email',
                              Lead.notes.ilike(f'%{NEEDLE}%'))
        rows = q.all()
        print(f'Leads that would be deleted: {len(rows)}')
        for l in rows:
            print(f'  #{l.id}  {l.company or "-":30s}  {l.pic or "-":25s}  {l.email or "-"}')
        if not rows:
            print('Nothing to purge.')
            return

        if not apply_changes:
            print('\nDRY RUN — re-run with --apply to actually delete.')
            return

        lead_ids = [l.id for l in rows]

        # Delete children first: attachments, email events pointing to these leads.
        att_deleted = LeadAttachment.query.filter(
            LeadAttachment.lead_id.in_(lead_ids)).delete(synchronize_session=False)
        evt_updated = EmailEvent.query.filter(
            EmailEvent.lead_id.in_(lead_ids)).update(
            {EmailEvent.lead_id: None, EmailEvent.status: 'lead_deleted'},
            synchronize_session=False)
        lead_deleted = q.delete(synchronize_session=False)

        db.session.commit()
        print(f'  [OK] Deleted {lead_deleted} leads · {att_deleted} attachments · '
              f'{evt_updated} email events unlinked (kept as history).')


if __name__ == '__main__':
    main(apply_changes='--apply' in sys.argv)
