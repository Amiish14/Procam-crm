"""
What re-running intake over the old mailbox items would do.

REPORT MODE ONLY. This creates nothing, attaches nothing, reopens
nothing and sends nothing. It reads the mailbox through Graph, runs
each message past the current classifier and prints — or writes as
CSV — what the CRM *would* do with it today.

The brief asks for 1 Sep to 5 Oct, the window before the ingest log
existed, where the CRM's own record of what happened is simply
absent.

    .venv/bin/python scripts/mail_backfill_report.py --check
    .venv/bin/python scripts/mail_backfill_report.py \\
        --from 2026-09-01 --to 2026-10-05 --csv /tmp/backfill.csv

Why it will not apply
    Applying means creating leads from six-week-old mail. Some of
    those enquiries have been answered by hand since, some have been
    created manually, and some are dead. Which is which is a
    judgement, and the CSV is what a person needs to make it. There
    is no --apply, deliberately: adding one would be the whole risk
    of this exercise in a single flag.

Duplicate safety
    Every row is checked against what the CRM already holds — by
    message id, by the client's address, and by subject — so a lead
    somebody created by hand (#12173 for BrahMos, from
    karansampath@brahmos.com) shows as ALREADY HELD rather than as
    something to create.
"""
import argparse
import csv
import os
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402

DEFAULT_FROM = '2026-09-01'
DEFAULT_TO = '2026-10-05'


def _already_held(msg, from_addr, subject):
    """Has the CRM already got this, by any of three routes?"""
    from app.models.ingest_log import MailIngestLog
    from app.services import lead_intake_db as db_ctx

    imid = (msg.get('internetMessageId') or '').strip()
    if imid:
        if Lead.query.filter_by(email_message_id=imid).first():
            return 'a lead already carries this message id'
        row = MailIngestLog.query.filter_by(
            internet_message_id=imid).first()
        if row is not None and row.outcome in ('created', 'attached',
                                               'reopened'):
            return f'the ingest log already records it as {row.outcome}'

    if from_addr:
        hit = Lead.query.filter(db.func.lower(Lead.email)
                                == from_addr.lower()).first()
        if hit is not None:
            return f'lead #{hit.id} already holds {from_addr}'

    try:
        match = db_ctx.find_by_subject(subject=subject,
                                       counterparties=[from_addr] if from_addr
                                       else (), within_days=180)
        if match:
            return f'subject matches lead #{match}'
    except Exception:
        db.session.rollback()
    return ''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--from', dest='since', default=DEFAULT_FROM)
    ap.add_argument('--to', dest='until', default=DEFAULT_TO)
    ap.add_argument('--csv')
    ap.add_argument('--limit', type=int, default=1000)
    ap.add_argument('--check', action='store_true',
                    help='say what it would read, without reading the '
                         'mailbox')
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        print(f'window:   {args.since} to {args.until}')
        print('MODE:     REPORT ONLY — nothing is created, attached, '
              'reopened or sent\n')

        if args.check:
            print(f'  Would read {os.environ.get("EMAIL_INGEST_MAILBOX") or "(EMAIL_INGEST_MAILBOX is not set)"} '
                  f'over that window and run each message past the '
                  f'current classifier.')
            print('  Needs Graph credentials in the environment. Nothing '
                  'is written either way.')
            return

        from app.services import lead_intake as li
        from app.services import lead_intake_db as db_ctx
        from email_ingest import graph_client

        mailbox = os.environ.get('EMAIL_INGEST_MAILBOX')
        if not mailbox:
            print('  EMAIL_INGEST_MAILBOX is not set, so there is no '
                  'mailbox to read. Nothing was written.')
            return
        until = datetime.fromisoformat(args.until)
        try:
            client = graph_client.GraphClient()
            # The client takes a start and pages forward; the end of
            # the window and the cap are applied here.
            messages = []
            for msg in client.list_messages(
                    mailbox=mailbox,
                    since_utc=datetime.fromisoformat(args.since),
                    top=100):
                received = str(msg.get('receivedDateTime') or '')[:19]
                if received and received > until.isoformat():
                    continue
                messages.append(msg)
                if len(messages) >= args.limit:
                    break
        except Exception as exc:                          # noqa: BLE001
            print(f'  Could not read the mailbox: {str(exc)[:200]}')
            print('  (Graph credentials, or the mailbox is unreachable. '
                  'Nothing was written.)')
            return

        ctx = db_ctx.build_context()
        rows, tally = [], {}
        for msg in messages:
            subject = (msg.get('subject') or '').strip()
            sender = (((msg.get('from') or {}).get('emailAddress') or {})
                      .get('address') or '')
            try:
                decision = li.classify(msg, ctx)
            except Exception as exc:                      # noqa: BLE001
                db.session.rollback()
                rows.append({'received': '', 'from': sender,
                             'subject': subject[:120],
                             'would': 'ERROR', 'lead_id': '',
                             'stage': '', 'reason': str(exc)[:120],
                             'already_held': ''})
                continue

            held = _already_held(msg, sender, subject)
            if held:
                would = 'ALREADY HELD'
            elif decision.creates_lead:
                would = 'CREATE'
            elif decision.lead_id:
                lead = db.session.get(Lead, decision.lead_id)
                closed = (lead is not None
                          and (lead.stage or '') in
                          ('Lost', 'Not Interested', 'On Hold', 'Won'))
                would = 'REOPEN' if closed else 'ATTACH'
            else:
                would = 'SKIP'
            tally[would] = tally.get(would, 0) + 1
            rows.append({
                'received': str(msg.get('receivedDateTime') or '')[:16],
                'from': sender,
                'subject': subject[:120],
                'would': would,
                'lead_id': decision.lead_id or '',
                'stage': (decision.create_stage
                          if decision.creates_lead else ''),
                'reason': f'{decision.klass}: {decision.reason}'[:160],
                'already_held': held,
            })

        print(f'{len(rows)} message(s)\n')
        for key in sorted(tally, key=lambda k: -tally[k]):
            print(f'  {key:<14} {tally[key]:>5}')
        print()
        for r in rows[:60]:
            print(f'  {r["would"]:<14} {r["received"]:<16} '
                  f'{r["subject"][:52]:<52} {r["reason"][:60]}')
        if len(rows) > 60:
            print(f'  ... and {len(rows) - 60} more')

        if args.csv and rows:
            with open(args.csv, 'w', newline='', encoding='utf-8') as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
            print(f'\n  written to {args.csv}')

        print('\nREPORT ONLY. Nothing was created, attached, reopened or '
              'sent. There is no --apply: which six-week-old enquiries '
              'are still live is a judgement, and this CSV is what makes '
              'it.')


if __name__ == '__main__':
    main()
