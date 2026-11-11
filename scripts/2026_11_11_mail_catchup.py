"""Mail the webhook never processed. Find it, then process it.

CRM-MAIL-2026-11-11, the item still open from CRM-WA-01: nothing has
created or attached a lead from leads@ since 2026-11-03 16:39,
although the Graph subscription is valid.

    .venv/bin/python scripts/2026_11_11_mail_catchup.py --check
    .venv/bin/python scripts/2026_11_11_mail_catchup.py --check --hours 240
    .venv/bin/python scripts/2026_11_11_mail_catchup.py --apply --hours 240

WHY A VALID SUBSCRIPTION IS NOT AN ANSWER

Graph calls the webhook; the webhook fetches the message and runs the
same `process_single_message` the poll uses. Between "Graph called"
and "a lead appeared" there are four places a message stops, and only
the last one writes to the ingest log:

    clientState mismatch   EMAIL_WEBHOOK_SECRET differs from the one
                           the subscription was created with. Every
                           notification is rejected.
    tenant mismatch        MS_TENANT_ID differs from the tenant the
                           notification names.
    wrong mailbox          the resource does not name leads@ and the
                           scoped fetch 404s.
    fetch failed           Graph would not return the message.

All four write an `EmailEvent` row and none writes a `MailIngestLog`
row — which is why `mail_ingest_status.py` reported nothing seen at
all. That script now reads both (see its --check output); this one
closes the gap the other way round, by going to Graph and asking
what we were sent.

WHAT --check DOES

Lists every message in the mailbox since `--hours` ago, and for each
says whether the CRM holds it: a lead carrying its
`internetMessageId`, a trail row, or an ingest-log row. Anything with
none of those was never processed, whatever the reason, and that list
is the backlog.

WHAT --apply DOES

Hands each unprocessed message to `process_single_message`, the
webhook's own function. That is idempotent by construction — it
returns 'already ingested' for anything we hold — and it logs every
outcome, so a catch-up cannot create a second copy of a lead and
cannot drop a message silently.

Deliberately NOT `pipeline.run_ingest`: that path carries its own
per-domain dedup, which skips a second enquiry from a customer who
wrote within thirty days. Right for a daily poll, wrong for a
catch-up — it would quietly drop exactly the leads this is for.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import Lead, LeadEmail, app, db                    # noqa: E402


def _rule(n=70):
    print('─' * n)


def _held(imid):
    """Does the CRM hold this message, by any of three records?"""
    if not imid:
        return ''
    if db.session.query(Lead.id).filter(
            Lead.email_message_id == imid).first():
        return 'a lead carries it'
    if db.session.query(LeadEmail.id).filter(
            LeadEmail.message_id == imid).first():
        return 'on a lead\'s trail'
    try:
        from app.models.ingest_log import MailIngestLog
        if db.session.query(MailIngestLog.id).filter(
                MailIngestLog.internet_message_id == imid).first():
            return 'in the ingest log'
    except Exception:                                     # noqa: BLE001
        pass
    return ''


def fetch(hours):
    """Every message in the mailbox since `hours` ago."""
    from email_ingest import service as mail_service
    from email_ingest.graph_client import GraphClient

    mailbox = mail_service.crm_inbox_email()
    if not mailbox:
        print('  !! no mailbox configured (CRM_INBOX_EMAIL).')
        raise SystemExit(2)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    graph = GraphClient()
    print(f'  mailbox : {mailbox}')
    print(f'  since   : {since.strftime("%Y-%m-%d %H:%M")}Z '
          f'({hours} hour(s))')
    try:
        return graph, mailbox, list(graph.list_messages(
            mailbox=mailbox, since_utc=since, top=100))
    except Exception as exc:                              # noqa: BLE001
        print(f'  !! Graph would not list the mailbox: '
              f'{type(exc).__name__}: {str(exc)[:200]}')
        print('     That is the answer, not a failure of this script: '
              'if the CRM cannot read the mailbox, nothing can be '
              'ingested from it. Check the app registration and '
              'MS_CLIENT_SECRET.')
        raise SystemExit(2)


def report(messages):
    print(f'\n── {len(messages)} message(s) in the window ──')
    held, missing = [], []
    for msg in messages:
        imid = (msg.get('internetMessageId') or '').strip()
        where = _held(imid)
        (held if where else missing).append((msg, where))
    print(f'  {len(held)} already in the CRM')
    print(f'  {len(missing)} NOT in the CRM — never processed')
    if held:
        print('\n  where the ones we hold are:')
        for where, n in Counter(w for _m, w in held).most_common():
            print(f'    {n:>5}  {where}')
    if missing:
        print('\n  the backlog, oldest first:')
        rows = sorted(missing,
                      key=lambda pair: pair[0].get('receivedDateTime') or '')
        for msg, _ in rows[:40]:
            sender = ((msg.get('from') or {}).get('emailAddress')
                      or {}).get('address') or ''
            print(f'    {(msg.get("receivedDateTime") or "")[:19]}  '
                  f'{sender[:34]:<34} {(msg.get("subject") or "")[:44]!r}')
        if len(rows) > 40:
            print(f'    … and {len(rows) - 40} more')
    return [msg for msg, where in missing if not where]


def webhook_side(hours):
    """What the webhook recorded — the half the ingest log cannot see."""
    print('\n── what the webhook itself recorded ──')
    try:
        from app import EmailEvent
        since = datetime.utcnow() - timedelta(hours=hours)
        rows = (EmailEvent.query
                .filter(EmailEvent.received_at >= since)
                .order_by(EmailEvent.id.desc()).all())
    except Exception as exc:                              # noqa: BLE001
        print(f'  could not read EmailEvent ({type(exc).__name__}).')
        return
    if not rows:
        print('  nothing at all. Graph did not call the webhook in this '
              'window — so the subscription, the notificationUrl or '
              'the route is the problem, not the classifier.')
        return
    print(f'  {len(rows)} event(s).')
    for status, n in Counter((r.status or '?') for r in rows).most_common():
        print(f'    {n:>5}  {status}')
    bad = [r for r in rows
           if (r.status or '') in ('rejected', 'failed')]
    if bad:
        print('\n  !! the most recent refusals, with their reason. A '
              '"clientState mismatch" means EMAIL_WEBHOOK_SECRET is '
              'not the secret the subscription was created with.')
        for row in bad[:10]:
            print(f'    {str(row.received_at)[:19]}  {row.status:<9} '
                  f'{(row.reason or "")[:80]}')


def apply(graph, mailbox, messages):
    """Hand each one to the webhook's own processor."""
    from email_ingest.single_message import process_single_message

    counts = Counter()
    for msg in sorted(messages,
                      key=lambda m: m.get('receivedDateTime') or ''):
        imid = (msg.get('internetMessageId') or '').strip()
        try:
            out = process_single_message(graph, mailbox, msg)
            status = (out or {}).get('status') or 'no status'
        except Exception as exc:                          # noqa: BLE001
            status = f'raised {type(exc).__name__}'
        counts[status] += 1
        print(f'  {(msg.get("receivedDateTime") or "")[:19]}  '
              f'{status:<12} {(msg.get("subject") or "")[:48]!r}')
    print('\n── done ──')
    for status, n in counts.most_common():
        print(f'  {n:>5}  {status}')
    print('  Everything here is also on Admin → Mail ingest, with its '
          'reason. Nothing was created twice: the processor refuses a '
          'message id the CRM already holds.')
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true',
                    help='report only. The default.')
    ap.add_argument('--apply', action='store_true',
                    help='process the backlog through the webhook\'s '
                         'own function')
    ap.add_argument('--hours', type=int, default=48,
                    help='how far back to look (default 48)')
    args = ap.parse_args(argv)

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('CRM-MAIL-2026-11-11 — mail the webhook never processed.'
              + ('' if args.apply else ' REPORT ONLY.'))
        _rule()
        webhook_side(args.hours)
        graph, mailbox, messages = fetch(args.hours)
        backlog = report(messages)
        if not args.apply:
            print(f'\nDRY RUN — nothing written. --apply --hours '
                  f'{args.hours} processes the {len(backlog)} message(s) '
                  f'above.')
            db.session.rollback()
            return 0
        if not backlog:
            print('\nNothing to process.')
            return 0
        apply(graph, mailbox, backlog)
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
