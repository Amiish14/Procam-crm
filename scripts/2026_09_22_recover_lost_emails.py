"""
Re-fetch the original enquiry for leads whose notes overwrote it.

Seventeen leads on production had their inbound email destroyed by the
Notes / call summary box before that bug was fixed.  Each still carries
its email_message_id, so the message can be read back out of the leads
mailbox and put where it belongs.

What it does
    1. Finds leads flagged by the split migration as probably damaged —
       original_email_source = 'migrated_from_notes' and a body that
       does not read like an email — or the explicit ids you pass.
    2. Looks each one up in the mailbox by internetMessageId.
    3. Writes the real body to original_email_body and the trail, and
       marks the source 'recovered_from_mailbox'.

    The text currently on the lead is already preserved as a flagged
    note by the split migration, so recovery adds the email back without
    taking the note away.

Safety
    Only leads the split migration flagged are ever touched; --ids narrows
    that set and cannot add a healthy lead to it. The trail row replaced is
    the one for this message (or the damaged one with no message id),
    never a later reply. Before committing, every value about to change is
    written to backups/recovery_preimage_<timestamp>.json.

Usage
    python scripts/2026_09_22_recover_lost_emails.py --check
    python scripts/2026_09_22_recover_lost_emails.py --ids 10440,11102
    python scripts/2026_09_22_recover_lost_emails.py --all
    python scripts/2026_09_22_recover_lost_emails.py --rollback \
        backups/recovery_preimage_<timestamp>.json

Needs the same Graph credentials the ingest uses.  --check contacts the
mailbox but writes nothing, so it is the safe way to find out how many
are actually retrievable before committing to anything.
"""
import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, '.env'))
except ImportError:
    pass

_EMAIL_MARKERS = re.compile(
    r'(^|\n)\s*(from|to|sent|subject|cc)\s*:|dear\s|regards|thanks\s*&|'
    r'sincerely|unsubscribe|@[\w.-]+\.\w+', re.I)


def looks_like_an_email(body):
    if not body:
        return False
    if len(body) > 400:
        return True
    return bool(_EMAIL_MARKERS.search(body))


def _html_to_text(html):
    """Graph returns HTML bodies; the CRM stores plain text."""
    if not html:
        return ''
    text = re.sub(r'(?is)<(script|style).*?</\1>', ' ', html)
    text = re.sub(r'(?i)<br\s*/?>', '\n', text)
    text = re.sub(r'(?i)</(p|div|tr|h[1-6])>', '\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    for entity, char in (('&nbsp;', ' '), ('&amp;', '&'), ('&lt;', '<'),
                         ('&gt;', '>'), ('&quot;', '"'), ('&#39;', "'")):
        text = text.replace(entity, char)
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n\s*\n\s*\n+', '\n\n', text).strip()


def damaged_leads(Lead, ids=None):
    """Leads whose enquiry the split migration flagged as overwritten.

    --ids narrows this set; it never widens it. Passing the id of a
    healthy lead used to overwrite its real original email with whatever
    the mailbox returned, so an id outside the flagged set is refused.
    Explicit ids do skip the looks-like-an-email heuristic — that is what
    they are for, when the heuristic misjudged a short damaged body.
    """
    q = Lead.query.filter(Lead.email_message_id.isnot(None),
                          Lead.email_message_id != '',
                          Lead.original_email_source == 'migrated_from_notes')
    if ids:
        return q.filter(Lead.id.in_(ids)).all()
    return [r for r in q.all()
            if not looks_like_an_email(r.original_email_body)]


def _trail_row(LeadEmail, lead, imid):
    """The trail row this message belongs in.

    The row carrying this message id, or failing that an inbound row with
    no message id (the damaged original). Never a different message — a
    lead can hold later customer replies, and those are real emails."""
    rows = LeadEmail.query.filter_by(lead_id=lead.id,
                                     direction='inbound').all()
    for row in rows:
        if row.message_id == imid:
            return row
    for row in rows:
        if not row.message_id:
            return row
    return None


def _save_preimage(records):
    """Everything about to be overwritten, to a file, before the commit.
    Rollback is a matter of writing these values back."""
    import json
    folder = os.path.join(_ROOT, 'backups')
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, 'recovery_preimage_'
                        + datetime.now().strftime('%Y%m%d-%H%M%S') + '.json')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as fh:
        json.dump(records, fh, indent=1, default=str)
    return path


def rollback(path, Lead, LeadEmail, db):
    """Put back exactly what a recovery run replaced, from its preimage.

    Only touches leads still marked recovered_from_mailbox — a lead edited
    by someone since is reported and left alone. A trail row the run
    created (no preimage row) is removed; one it changed is restored."""
    import json
    with open(path) as fh:
        records = json.load(fh)
    restored, skipped = 0, []
    for rec in records:
        lead = db.session.get(Lead, rec['lead_id'])
        if lead is None or lead.original_email_source != \
                'recovered_from_mailbox':
            skipped.append(rec['lead_id'])
            continue
        for field in ('original_email_body', 'original_email_subject',
                      'original_email_from', 'original_email_source'):
            setattr(lead, field, rec[field])
        before = rec.get('lead_email')
        if before is None:
            LeadEmail.query.filter_by(
                lead_id=lead.id, message_id=rec['message_id'],
                source='recovered_from_mailbox').delete(
                synchronize_session=False)
        else:
            row = db.session.get(LeadEmail, before['id'])
            if row is not None:
                for field in ('body', 'subject', 'from_addr', 'source',
                              'status', 'message_id'):
                    setattr(row, field, before[field])
        restored += 1
    db.session.commit()
    return restored, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true',
                    help='look the messages up but write nothing')
    ap.add_argument('--ids', help='comma-separated lead ids to recover')
    ap.add_argument('--all', action='store_true',
                    help='every lead the split migration flagged')
    ap.add_argument('--lookback-days', type=int, default=400,
                    help='how far back to search the mailbox')
    ap.add_argument('--rollback', metavar='PREIMAGE',
                    help='undo a run, from the preimage file it saved')
    args = ap.parse_args()

    if args.rollback:
        from app import app as flask_app, db, Lead, LeadEmail  # noqa: E402
        with flask_app.app_context():
            n, skipped = rollback(args.rollback, Lead, LeadEmail, db)
        print(f'  restored {n} lead(s) to their pre-recovery state')
        if skipped:
            print(f'  left alone (changed since, or gone): '
                  f'{", ".join(map(str, skipped))}')
        return

    if not (args.check or args.ids or args.all):
        raise SystemExit('Pass --check, --ids, --all or --rollback.')

    ids = None
    if args.ids:
        ids = [int(x) for x in args.ids.split(',') if x.strip().isdigit()]
        if not ids:
            raise SystemExit('--ids needs comma-separated numeric lead ids.')

    from app import app as flask_app, db, Lead, LeadEmail    # noqa: E402

    # The mailbox the webhook ingests from. EMAIL_INGEST_MAILBOX is the
    # legacy poller's name for it and still wins when set.
    from email_ingest import service as _mail
    mailbox = os.environ.get('EMAIL_INGEST_MAILBOX') or _mail.crm_inbox_email()
    if not mailbox:
        raise SystemExit('No mailbox configured (CRM_INBOX_EMAIL) — nothing '
                         'to search.')
    print(f'mailbox: {mailbox}')

    with flask_app.app_context():
        targets = damaged_leads(Lead, ids)
        print(f'leads to recover: {len(targets)}')
        if ids:
            refused = sorted(set(ids) - {t.id for t in targets})
            if refused:
                print(f'  refused (not flagged as damaged, or no message id): '
                      f'{", ".join(map(str, refused))}')
        if not targets:
            return
        wanted = {}
        for lead in targets:
            wanted.setdefault(lead.email_message_id, []).append(lead)

        from email_ingest.graph_client import GraphClient
        client = GraphClient()
        since = datetime.now(timezone.utc) - timedelta(days=args.lookback_days)

        print(f'searching the mailbox back to {since:%Y-%m-%d} …')
        found = {}
        scanned = 0
        for msg in client.list_messages(mailbox, since_utc=since, top=100):
            scanned += 1
            imid = msg.get('internetMessageId')
            if imid and imid in wanted and imid not in found:
                found[imid] = msg
                if len(found) == len(wanted):
                    break
        print(f'scanned {scanned} message(s); matched {len(found)} '
              f'of {len(wanted)}')

        recovered = 0
        preimage = []
        for imid, leads in wanted.items():
            msg = found.get(imid)
            for lead in leads:
                if not msg:
                    print(f'  lead #{lead.id:<6} {(lead.company or "")[:26]:<28} '
                          f'NOT FOUND in the mailbox')
                    continue
                body = _html_to_text((msg.get('body') or {}).get('content')) \
                    or msg.get('bodyPreview') or ''
                if not body:
                    print(f'  lead #{lead.id:<6} message found but empty')
                    continue
                print(f'  lead #{lead.id:<6} {(lead.company or "")[:26]:<28} '
                      f'{"WOULD recover" if args.check else "recovered"} '
                      f'{len(body)} chars')
                if args.check:
                    continue
                sender = ((msg.get('from') or {}).get('emailAddress')
                          or {}).get('address')
                row = _trail_row(LeadEmail, lead, imid)
                preimage.append({
                    'lead_id': lead.id, 'message_id': imid,
                    'original_email_body': lead.original_email_body,
                    'original_email_subject': lead.original_email_subject,
                    'original_email_from': lead.original_email_from,
                    'original_email_source': lead.original_email_source,
                    'lead_email': None if row is None else {
                        'id': row.id, 'body': row.body,
                        'subject': row.subject, 'from_addr': row.from_addr,
                        'source': row.source, 'status': row.status,
                        'message_id': row.message_id},
                })
                lead.original_email_body = body[:8000]
                lead.original_email_subject = (msg.get('subject') or '')[:500]
                lead.original_email_from = (sender or '')[:320] or None
                lead.original_email_source = 'recovered_from_mailbox'
                if row is None:
                    row = LeadEmail(lead_id=lead.id, direction='inbound',
                                    message_id=imid)
                    db.session.add(row)
                row.message_id = row.message_id or imid
                row.body = body[:8000]
                row.subject = lead.original_email_subject
                row.from_addr = lead.original_email_from
                row.source = 'recovered_from_mailbox'
                row.status = 'received'
                recovered += 1

        if args.check:
            print('\n== nothing written ==')
        else:
            if preimage:
                path = _save_preimage(preimage)
                print(f'\n  previous values saved to {path}')
            db.session.commit()
            print(f'\n  recovered {recovered} lead(s). The text that had '
                  f'overwritten them\n  remains as a flagged note — nothing '
                  f'was taken away.')


if __name__ == '__main__':
    main()
