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

Usage
    python scripts/2026_09_22_recover_lost_emails.py --check
    python scripts/2026_09_22_recover_lost_emails.py --ids 10440,11102
    python scripts/2026_09_22_recover_lost_emails.py --all

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
    q = Lead.query.filter(Lead.email_message_id.isnot(None),
                          Lead.email_message_id != '')
    if ids:
        return q.filter(Lead.id.in_(ids)).all()
    rows = q.filter(Lead.original_email_source == 'migrated_from_notes').all()
    return [r for r in rows if not looks_like_an_email(r.original_email_body)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true',
                    help='look the messages up but write nothing')
    ap.add_argument('--ids', help='comma-separated lead ids to recover')
    ap.add_argument('--all', action='store_true',
                    help='every lead the split migration flagged')
    ap.add_argument('--lookback-days', type=int, default=400,
                    help='how far back to search the mailbox')
    args = ap.parse_args()

    if not (args.check or args.ids or args.all):
        raise SystemExit('Pass --check, --ids or --all.')

    ids = None
    if args.ids:
        ids = [int(x) for x in args.ids.split(',') if x.strip().isdigit()]

    from app import app as flask_app, db, Lead, LeadEmail    # noqa: E402

    mailbox = os.environ.get('EMAIL_INGEST_MAILBOX')
    if not mailbox:
        raise SystemExit('EMAIL_INGEST_MAILBOX is not set — nothing to '
                         'search.')
    print(f'mailbox: {mailbox}')

    with flask_app.app_context():
        targets = damaged_leads(Lead, ids)
        print(f'leads to recover: {len(targets)}')
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
                lead.original_email_body = body[:8000]
                lead.original_email_subject = (msg.get('subject') or '')[:500]
                lead.original_email_from = (sender or '')[:320] or None
                lead.original_email_source = 'recovered_from_mailbox'
                row = LeadEmail.query.filter_by(
                    lead_id=lead.id, direction='inbound').first()
                if row is None:
                    row = LeadEmail(lead_id=lead.id, direction='inbound',
                                    message_id=imid)
                    db.session.add(row)
                row.body = body[:8000]
                row.subject = lead.original_email_subject
                row.from_addr = lead.original_email_from
                row.source = 'recovered_from_mailbox'
                row.status = 'received'
                recovered += 1

        if args.check:
            print('\n== nothing written ==')
        else:
            db.session.commit()
            print(f'\n  recovered {recovered} lead(s). The text that had '
                  f'overwritten them\n  remains as a flagged note — nothing '
                  f'was taken away.')


if __name__ == '__main__':
    main()
