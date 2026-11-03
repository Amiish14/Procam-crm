"""What the CRM would read out of a forward-as-attachment. READ ONLY.

CRM-USER-ISSUES-02NOV §4, second pass.

    .venv/bin/python scripts/2026_11_03_attached_forwards.py --check
    .venv/bin/python scripts/2026_11_03_attached_forwards.py --check --limit 50
    .venv/bin/python scripts/2026_11_03_attached_forwards.py --check --full
    .venv/bin/python scripts/2026_11_03_attached_forwards.py --check \
          --message '<AM0PR...@eurprd.outlook.com>'

IT CREATES NOTHING, EVER

There is no `--apply`. The only thing this script can do is read the
mailbox and print. It does not write a lead, a timeline entry, an
ingest-log row or a flag, and it rolls the session back before it
exits so a stray read cannot leave one either. `--check` is required
rather than default so that running it by accident does nothing at all.

WHAT IT ANSWERS

For each of the last N messages in the leads mailbox:

    * does it carry an attached message, and how many;
    * who the attached message is really from, and what its subject is
      — which is the lead's sender and subject once this ships, and
      was the colleague's covering note before;
    * what the parser extracts from the spliced result: company,
      contact, route, cargo, and whether it reads as an enquiry;
    * and what the CRM has on file for it today, so the difference is
      visible rather than asserted.

The last column is the point. A message that already became a lead
from its covering note shows the old sender beside the new one, and
that is the before-and-after for the fix on real mail.

WHY IT COSTS A GRAPH CALL PER MESSAGE

`$expand=microsoft.graph.itemAttachment/item` is the only way to see
inside an attached message, and it is per-message. Fifty messages is
fifty calls, which is seconds. It is gated on `hasAttachments` first,
so most of the fifty cost nothing.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                              # noqa: E402

from email_ingest import attached_message as att           # noqa: E402
from email_ingest import parser as email_parser            # noqa: E402
from email_ingest import service as mail_service           # noqa: E402
from email_ingest.graph_client import GraphClient          # noqa: E402


def _existing(imid):
    """What the CRM holds for this message today, if anything."""
    if not imid:
        return None
    return Lead.query.filter_by(email_message_id=imid).first()


def _one(msg, graph, mailbox, *, full=False):
    imid = (msg.get('internetMessageId') or '').strip()
    outer_from = (((msg.get('from') or {}).get('emailAddress') or {})
                  .get('address') or '')
    out = {
        'imid': imid,
        'subject': (msg.get('subject') or '').strip(),
        'outer_from': outer_from,
        'received': (msg.get('receivedDateTime') or '')[:16],
        'has_attachments': bool(msg.get('hasAttachments')),
        'lead': _existing(imid),
        'report': None,
        'extracted': None,
    }

    # A copy, so nothing this script does can be mistaken for the
    # ingest having run. `unwrap` mutates what it is given, on purpose.
    probe = dict(msg)
    report = att.unwrap(probe, graph, mailbox)
    out['report'] = report
    if not report.get('spliced'):
        return out

    try:
        out['extracted'] = email_parser.extract_lead(probe)
    except Exception as exc:                              # noqa: BLE001
        out['extracted'] = {'skip_reason': f'parser raised: {exc}'}
    out['body'] = email_parser._get_body_text(probe) if full else ''
    return out


def _print(row, *, full=False):
    report = row['report'] or {}
    found = report.get('found') or 0

    print(f'\n── {row["received"]}  {row["subject"][:66]!r}')
    print(f'   outer from : {row["outer_from"] or "—"}')

    if not row['has_attachments']:
        print('   attachments: none')
        return
    if not report.get('spliced'):
        print(f'   attached   : {found} message(s) — not read '
              f'({report.get("skipped") or "no reason given"})')
        return

    first = (report.get('messages') or [{}])[0]
    print(f'   attached   : {found} message(s)'
          + ('  !! only the first is read' if found > 1 else ''))
    print(f'   REAL from  : {first.get("from") or "—"}'
          + (f'  ({first.get("from_name")})' if first.get('from_name')
             else ''))
    print(f'   REAL subj  : {first.get("subject") or "—"!r}')
    print(f'   sent       : {first.get("sent") or "—"}')
    print(f'   body       : {first.get("body_chars", 0)} chars')

    ex = row.get('extracted') or {}
    signals = ex.get('signals') or {}
    print(f'   would read : company={ex.get("company") or "—"!r} '
          f'contact={ex.get("contact_name") or "—"!r} '
          f'email={ex.get("email") or "—"}')
    print(f'                route={signals.get("origin") or "?"}'
          f' → {signals.get("destination") or "?"}'
          f'  cargo={signals.get("cargo_keywords") or []}'
          f'  rfq={signals.get("rfq")}'
          f'  confidence={ex.get("confidence")}')
    if ex.get('skip_reason'):
        print(f'                triage label: {ex["skip_reason"]}')

    lead = row['lead']
    if lead is None:
        print('   on file    : no lead for this message')
    else:
        print(f'   on file    : lead #{lead.id} — '
              f'company={(lead.company or "")[:32]!r} '
              f'from={(lead.original_email_from or "")[:40]} '
              f'stage={lead.stage}')
        if (first.get('from') or '').lower() not in (
                (lead.original_email_from or '').lower()):
            print('                !! the lead names a different sender '
                  'than the attached message — this is the case the fix '
                  'is for')
    if full and row.get('body'):
        print('   ── what the parser would see ──')
        for line in (row['body'] or '').splitlines()[:40]:
            print(f'   | {line[:110]}')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true', required=True,
                    help='required: this script only ever reads')
    ap.add_argument('--limit', type=int, default=50,
                    help='how many recent messages to look at '
                         '(default 50)')
    ap.add_argument('--hours', type=int, default=24 * 14,
                    help='how far back to look (default 14 days)')
    ap.add_argument('--full', action='store_true',
                    help='also print the text the parser would see')
    ap.add_argument('--message', action='append', default=None,
                    metavar='INTERNET_MESSAGE_ID',
                    help='one specific message instead of the recent '
                         'ones; repeatable')
    ap.add_argument('--only-attached', action='store_true',
                    help='print only the messages that carry one')
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        mailbox = mail_service.crm_inbox_email()
        print(f'mailbox : {mailbox}')
        print('CRM-USER-ISSUES-02NOV §4 — forward-as-attachment. '
              'READ ONLY: nothing is created.')

        graph = GraphClient()

        if args.message:
            from email_ingest.webhook import _get_message_by_internet_id
            messages = []
            for wanted in args.message:
                found = _get_message_by_internet_id(graph, mailbox, wanted)
                if found is None:
                    print(f'  !! {wanted} is not in the mailbox')
                else:
                    messages.append(found)
        else:
            since = datetime.utcnow() - timedelta(hours=args.hours)
            messages = []
            for msg in graph.list_messages(mailbox=mailbox,
                                           since_utc=since, top=50):
                messages.append(msg)
                if len(messages) >= args.limit:
                    break

        print(f'\n{len(messages)} message(s) to look at.')

        carried = 0
        differing = 0
        for msg in messages:
            row = _one(msg, graph, mailbox, full=args.full)
            spliced = (row['report'] or {}).get('spliced')
            if spliced:
                carried += 1
                lead = row['lead']
                first = ((row['report'].get('messages') or [{}])[0])
                if lead is not None and (first.get('from') or '').lower() \
                        not in ((lead.original_email_from or '').lower()):
                    differing += 1
            if args.only_attached and not spliced:
                continue
            _print(row, full=args.full)

        print(f'\n─────────────────────────────────────────────')
        print(f'{carried} of {len(messages)} carried an attached message '
              f'the CRM could not read before.')
        if differing:
            print(f'{differing} of those already became a lead naming the '
                  f'wrong sender — the covering note rather than the '
                  f'customer.')
        print('Nothing was created. There is no --apply in this script; '
              'the fix itself is in the ingest and applies to mail that '
              'arrives after the deploy.')

        # Belt and braces: the reads above should not have dirtied
        # anything, and if something did, it does not survive.
        db.session.rollback()


if __name__ == '__main__':
    main()
