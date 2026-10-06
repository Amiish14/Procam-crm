"""
What the leads mailbox should have done since 1 September, and did not.

A dry run by default, and emphatically so: it reports what it *would*
do and writes nothing. Applying it needs `--apply --yes`, and even
then it sends no notification email — the notifications would be
telling people about work that happened weeks ago, which is noise
with somebody's name on it.

    .venv/bin/python scripts/repair_mail_backfill.py
    .venv/bin/python scripts/repair_mail_backfill.py --from 2026-09-01
    .venv/bin/python scripts/repair_mail_backfill.py --json report.json
    .venv/bin/python scripts/repair_mail_backfill.py --apply --yes

Each message is classified the way the live ingest would classify it
*now* — with the rules added since: reopening a closed lead, rescuing
an internal-only forward, reading the client's own date, and
deduplicating on the message id rather than the thread. So the report
is the difference the new rules make, which is the question.

    would create    a lead that does not exist
    would attach    filed against a lead that does
    would reopen    an enquiry against a lead somebody closed
    would skip      correctly declined
    error           could not be examined
"""
import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import app, db                                   # noqa: E402

DEFAULT_FROM = '2026-09-01'

#: Leads named in the brief as known casualties. Reported separately,
#: because "the backfill found 180 things" does not answer "did it
#: find the four we know about".
WATCHED = {
    '12001': 'Lead #12001 — expected PIC Suranjan Aon',
    '11874': 'Lead #11874 — expected RFQ Generated, PIC Suranjan Aon',
}
WATCHED_TERMS = (
    ('linde', 'Linde JSW Bellary / Paradip RFI'),
    ('jsw', 'Linde JSW Bellary / Paradip RFI'),
    ('shyam metalics', 'Shyam Metalics Sambalpur → Durgapur transformer '
                       'quotation'),
    ('sambalpur', 'Shyam Metalics Sambalpur → Durgapur transformer '
                  'quotation'),
)


def _iso(day):
    return datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=timezone.utc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--from', dest='since', default=DEFAULT_FROM)
    ap.add_argument('--to', dest='until')
    ap.add_argument('--limit', type=int, default=2000)
    ap.add_argument('--json', dest='json_path',
                    help='write the full report to this file')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    since = _iso(args.since)
    until = _iso(args.until) if args.until else datetime.now(timezone.utc)

    with app.app_context():
        from app import Lead
        from app.services import lead_intake as li, lead_intake_db as lidb
        from app.services import mail_ingest as mi
        from email_ingest import parser as email_parser
        from email_ingest import service as mail_service
        from email_ingest.graph_client import GraphClient

        mailbox = mail_service.crm_inbox_email()
        print(f'mailbox : {mailbox}')
        print(f'window  : {args.since} → {str(until)[:10]}')
        print(f'mode    : {"APPLY" if args.apply else "DRY RUN"}\n')

        graph = GraphClient()
        tally = Counter()
        rows, watched_hits = [], []
        context = lidb.build_context()

        for msg in graph.list_messages(mailbox=mailbox, since_utc=since):
            received = email_parser.received_datetime(msg)
            if received and received.replace(tzinfo=timezone.utc) > until:
                continue
            if len(rows) >= args.limit:
                print(f'(stopped at --limit {args.limit})')
                break

            imid = (msg.get('internetMessageId') or '').strip()
            subject = (msg.get('subject') or '')[:120]
            sender = (((msg.get('from') or {}).get('emailAddress') or {})
                      .get('address') or '')
            entry = {'message_id': imid, 'subject': subject,
                     'from': sender,
                     'received_at': str(received)[:16] if received else '',
                     'outcome': '', 'reason': '', 'lead_id': None}

            try:
                if mi.already_ingested(imid):
                    lead = Lead.query.filter_by(email_message_id=imid).first()
                    entry.update(outcome='already', lead_id=getattr(
                        lead, 'id', None), reason='already in the CRM')
                else:
                    decision = li.classify(msg, context)
                    entry['reason'] = f'{decision.klass}: {decision.reason}'
                    if decision.lead_id:
                        lead = db.session.get(Lead, decision.lead_id)
                        if mi.should_reopen(lead, decision.klass):
                            entry.update(outcome='would reopen',
                                         lead_id=decision.lead_id)
                        else:
                            entry.update(outcome='would attach',
                                         lead_id=decision.lead_id)
                    elif decision.creates_lead:
                        entry['outcome'] = 'would create'
                    elif decision.klass == li.Klass.INTERNAL:
                        found = mi.rescue_internal_forward(
                            msg, li.body_text(msg),
                            internal_domains=email_parser._skip_domains())
                        entry['outcome'] = 'would create'
                        entry['reason'] = (
                            f'internal forward — {found["how"]}; '
                            f'client: {found["company"] or "to review"}')
                    else:
                        entry['outcome'] = 'would skip'
            except Exception as exc:                      # noqa: BLE001
                entry.update(outcome='error', reason=str(exc)[:200])

            tally[entry['outcome']] += 1
            rows.append(entry)

            haystack = f'{subject} {sender}'.lower()
            for term, label in WATCHED_TERMS:
                if term in haystack:
                    watched_hits.append({**entry, 'watched': label})
                    break

        print('WHAT THE REPAIR WOULD DO')
        for outcome in ('would create', 'would attach', 'would reopen',
                        'would skip', 'already', 'error'):
            if tally.get(outcome):
                print(f'  {outcome:<14} {tally[outcome]:>5}')
        print(f'  {"examined":<14} {len(rows):>5}')

        print('\nTHE CASES NAMED IN THE BRIEF')
        for lead_id, label in WATCHED.items():
            lead = db.session.get(Lead, int(lead_id))
            if lead is None:
                print(f'  NOT FOUND   {label}')
            else:
                print(f'  lead {lead_id:<6} {label}')
                print(f'              stage={lead.stage!r} '
                      f'owner={lead.assigned_to or "nobody"!r}')
        if watched_hits:
            for hit in watched_hits[:20]:
                print(f'  {hit["outcome"]:<14} {hit["watched"]}')
                print(f'              {hit["subject"][:90]}')
        else:
            print('  no message in the window matched Linde/JSW or '
                  'Shyam Metalics by subject or sender.')

        print('\nFIRST FEW')
        for entry in rows[:15]:
            print(f'  {entry["outcome"]:<14} {entry["received_at"]}  '
                  f'{entry["subject"][:60]}')
            if entry['reason']:
                print(f'                 {entry["reason"][:100]}')

        if args.json_path:
            with open(args.json_path, 'w', encoding='utf-8') as fh:
                json.dump({'window': [args.since, str(until)[:10]],
                           'tally': dict(tally), 'rows': rows,
                           'watched': watched_hits}, fh, indent=2)
            print(f'\nfull report written to {args.json_path}')

        if not args.apply:
            print('\n== DRY RUN — nothing was written and nothing was '
                  'sent ==')
            print('Read the report, then re-run with --apply --yes. '
                  'Notification email stays off either way.')
            return 0

        if not args.yes:
            raise SystemExit('Refusing to apply without --yes.')
        return _apply(graph, mailbox, rows)


def _apply(graph, mailbox, rows):
    """Re-run the ingest for everything the dry run would act on.

    Inside `notify.silent()`: these notifications would be telling
    people about work from weeks ago, and a backfill that fills forty
    inboxes is a backfill nobody lets you run twice.
    """
    from app.services import notify
    from email_ingest.single_message import process_single_message
    from email_ingest.webhook import _get_message_by_internet_id

    actionable = [r for r in rows
                  if r['outcome'] in ('would create', 'would attach',
                                      'would reopen')]
    print(f'\napplying {len(actionable)} message(s), notifications off')
    done = Counter()
    with notify.silent() as would_have_notified:
        for entry in actionable:
            try:
                msg = _get_message_by_internet_id(graph, mailbox,
                                                  entry['message_id'])
                if not msg:
                    done['gone'] += 1
                    continue
                result = process_single_message(graph, mailbox, msg)
                done[(result or {}).get('status', 'unknown')] += 1
            except Exception as exc:                      # noqa: BLE001
                done['error'] += 1
                print(f'  {entry["subject"][:60]}: {exc}')
    for key, count in sorted(done.items()):
        print(f'  {key:<12} {count}')
    print(f'  {len(would_have_notified)} person(s) were NOT emailed, '
          f'deliberately.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
