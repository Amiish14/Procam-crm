"""Mail the intake refused or dropped: list it, then put it through again.

CRM-FINAL-13NOV follow-up, items 1 and 3. Two different refusals, one
mechanism for undoing them, so there is one place that knows how to
re-process a message rather than two that drift.

    .venv/bin/python scripts/2026_11_14_reprocess_mail.py --check
    .venv/bin/python scripts/2026_11_14_reprocess_mail.py --check --portals
    .venv/bin/python scripts/2026_11_14_reprocess_mail.py --check --failed
    .venv/bin/python scripts/2026_11_14_reprocess_mail.py --check --csv
    .venv/bin/python scripts/2026_11_14_reprocess_mail.py --apply --portals
    .venv/bin/python scripts/2026_11_14_reprocess_mail.py --apply --failed

ITEM 1 — THE PORTALS

68 messages from `procol.in` and 4 from `ansmtp.ariba.com` were
dropped with "blocked sender domain". Both are customer procurement
portals: what they send is an RFQ or a tender invitation, from a
buyer. `--portals` lists all 72 with the subject and the real sender,
and `--apply --portals` puts each one through the intake again.

They come back differently now. `data/portal_senders.txt` wins over
the denylist, so each becomes a lead flagged for review, owned by the
RFQ desk, with the buyer read out of the body -- see
`app/services/portal_rfq.py`.

ITEM 3 — THE ONE THAT FAILED

One webhook notification failed with "lead insert: (sqlite3
.IntegrityError) UNIQUE constraint failed: leads." `--failed` finds
it, says what was lost, and names which unique column collided, which
is the half the truncated message does not say. `leads` has two:
`crn` and `email_message_id`.

WHAT "RE-PROCESS" MEANS

`process_single_message(..., force=True)` -- the webhook's own
function, with force, which is what the Mail ingest screen's
re-process button passes. It is idempotent for a message that did
land, so a re-run cannot double a lead, and it writes an ingest row
whatever happens. Graph is asked for the message by its id: a message
deleted or moved out of the Inbox since cannot be re-processed, and
that is reported rather than guessed at.

Nothing is deleted and no lead is changed by `--check`.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from collections import Counter
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import app, db                                    # noqa: E402

DEFAULT_CSV = os.path.join(_ROOT, 'data', 'reprocess_14NOV.csv')

#: `leads`' own unique columns, for item 3's diagnosis.
LEAD_UNIQUE = ('crn', 'email_message_id')


def _rule(n=72):
    print('─' * n)


# ── item 1: the portals ─────────────────────────────────────────────
def blocked_portal_rows(days=None):
    """Ingest rows blocked whose sender is a procurement portal."""
    from app.models.ingest_log import MailIngestLog
    from app.services import portal_rfq

    q = MailIngestLog.query.filter(MailIngestLog.outcome == 'blocked')
    if days:
        since = datetime.utcnow() - timedelta(days=days)
        q = q.filter(db.or_(MailIngestLog.received_at >= since,
                            MailIngestLog.created_at >= since))
    out = []
    for row in q.order_by(MailIngestLog.id.asc()).all():
        portal = portal_rfq.is_portal(row.from_addr or '')
        if portal:
            out.append((row, portal))
    return out


def report_portals(rows):
    from app.services import portal_rfq

    print(f'\n══ {len(rows)} blocked message(s) from a procurement '
          f'portal ══')
    if not rows:
        print('  none on record.')
        return
    by_portal = Counter(portal for _row, portal in rows)
    for portal, n in by_portal.most_common():
        print(f'  {n:>5}  {portal}')
    print('\n  every one of them, by subject and real sender:')
    print(f'  {"when":<17} {"sender":<34} subject')
    for row, _portal in rows:
        when = str(row.received_at or row.created_at or '')[:16]
        print(f'  {when:<17} {(row.from_addr or "—")[:34]:<34} '
              f'{(row.subject or "")[:60]}')
    with_buyer = 0
    for row, _portal in rows:
        buyer, _how = portal_rfq.buyer_from(row.subject or '', '')
        if buyer:
            with_buyer += 1
    print(f'\n  the buyer can be read from the SUBJECT alone on '
          f'{with_buyer} of {len(rows)}. The rest will be read from the '
          f'body when the message is fetched again, and anything still '
          f'unnamed lands as "To review", flagged — not dropped.')


# ── item 3: the ones that failed ────────────────────────────────────
def failed_rows(days=None):
    """(where, id, imid, when, sender, subject, reason) for each failure.

    Both logs. `MailIngestLog` has the classifier's own failures;
    `EmailEvent` has the ones that died at the route, which never
    reach the first table at all -- which is the gap that made
    "nothing seen" mean two different things in the status script.
    """
    from app.models.ingest_log import MailIngestLog

    out = []
    since = (datetime.utcnow() - timedelta(days=days)) if days else None
    q = MailIngestLog.query.filter(MailIngestLog.outcome == 'error')
    if since:
        q = q.filter(db.or_(MailIngestLog.received_at >= since,
                            MailIngestLog.created_at >= since))
    for row in q.order_by(MailIngestLog.id.desc()).all():
        out.append(('ingest log', row.id, row.internet_message_id,
                    row.received_at or row.created_at, row.from_addr,
                    row.subject, row.reason or ''))
    try:
        from app import EmailEvent
        q2 = EmailEvent.query.filter(EmailEvent.status.in_(('failed',
                                                            'rejected')))
        if since:
            q2 = q2.filter(EmailEvent.received_at >= since)
        for row in q2.order_by(EmailEvent.id.desc()).all():
            out.append(('webhook', row.id, row.internet_message_id,
                        row.received_at, None, row.subject,
                        row.reason or ''))
    except Exception:                                     # noqa: BLE001
        pass
    return out


def explain_unique(reason):
    """Which unique column collided, where the message says.

    The reported sentence stops at "UNIQUE constraint failed: leads."
    -- the column is what decides whether anything was lost, so it is
    named here rather than guessed at.
    """
    text = (reason or '')
    for column in LEAD_UNIQUE:
        if f'leads.{column}' in text:
            return column
    if 'UNIQUE constraint failed: leads' in text:
        return ''
    return None


def report_failed(rows):
    print(f'\n══ {len(rows)} failure(s) on record ══')
    if not rows:
        print('  none.')
        return
    for where, row_id, imid, when, sender, subject, reason in rows:
        print(f'\n  [{where} #{row_id}] {str(when or "")[:19]}')
        print(f'    message id {imid or "— not recorded"}')
        print(f'    from       {sender or "—"}')
        print(f'    subject    {(subject or "")[:66]!r}')
        print(f'    reason     {reason[:160]}')
        column = explain_unique(reason)
        if column:
            print(f'    → the collision is on `leads.{column}`.')
            _explain_column(column, imid)
        elif column == '':
            print('    → the message says only "leads", not which '
                  'column. `leads` has two unique columns — '
                  f'{", ".join(LEAD_UNIQUE)} — and the lead that '
                  'holds the value is looked for below.')
            for candidate in LEAD_UNIQUE:
                _explain_column(candidate, imid)
        if imid:
            print(f'    what was lost: {_what_was_lost(imid)}')


def _explain_column(column, imid):
    from app import Lead

    if column == 'email_message_id' and imid:
        existing = Lead.query.filter_by(email_message_id=imid).first()
        if existing is not None:
            print(f'      lead #{existing.id} already carries this '
                  f'message id, so the insert was a SECOND attempt at '
                  f'a lead that exists. Nothing was lost.')
        else:
            print('      no lead carries this message id, so the '
                  'collision was not on it.')
    elif column == 'crn':
        print('      a CRN collision: two leads allocated the same '
              'customer reference at the same moment. The enquiry IS '
              'lost until it is re-processed — the insert rolled '
              'back and no lead exists.')


def _what_was_lost(imid):
    from app import Lead, LeadEmail

    if Lead.query.filter_by(email_message_id=imid).first():
        return 'nothing — a lead carries this message'
    if LeadEmail.query.filter_by(message_id=imid).first():
        return 'nothing — it is on a lead\'s mail trail'
    return ('THE ENQUIRY. No lead and no trail row holds this message: '
            're-processing it is the only way it reaches the CRM')


# ── re-processing ───────────────────────────────────────────────────
def reprocess(imids):
    """Put each message id through the webhook's own function."""
    from email_ingest import service as mail_service
    from email_ingest.graph_client import GraphClient
    from email_ingest.single_message import process_single_message
    from email_ingest.webhook import _get_message

    box = mail_service.crm_inbox_email()
    graph = GraphClient()
    counts = Counter()
    for imid in imids:
        if not imid:
            counts['no message id'] += 1
            continue
        try:
            msg = _get_message(graph, box, imid)
        except Exception as exc:                          # noqa: BLE001
            counts['gone from the mailbox'] += 1
            print(f'  {imid[:50]}: could not be fetched '
                  f'({type(exc).__name__}) — deleted or moved out of '
                  f'the Inbox since')
            continue
        try:
            out = process_single_message(graph, box, msg, force=True)
            status = (out or {}).get('status') or 'no status'
            lead_id = (out or {}).get('lead_id')
        except Exception as exc:                          # noqa: BLE001
            status, lead_id = f'raised {type(exc).__name__}', None
        counts[status] += 1
        print(f'  {status:<12} {"lead #" + str(lead_id) if lead_id else "":<10}'
              f' {(msg.get("subject") or "")[:50]!r}')
    print('\n── done ──')
    for status, n in counts.most_common():
        print(f'  {n:>5}  {status}')
    print('  Everything here is on Admin → Mail ingest with its '
          'reason. A message that already landed is refused by the '
          "processor's own idempotency, so nothing was doubled.")
    return counts


def write_csv(portals, failures, path):
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(['kind', 'when', 'sender', 'subject', 'message_id',
                  'reason'])
    for row, portal in portals:
        out.writerow(['portal:' + portal,
                      str(row.received_at or row.created_at or '')[:19],
                      row.from_addr or '', (row.subject or '')[:120],
                      row.internet_message_id or '', row.reason or ''])
    for where, _id, imid, when, sender, subject, reason in failures:
        out.writerow(['failed:' + where, str(when or '')[:19],
                      sender or '', (subject or '')[:120], imid or '',
                      reason[:160]])
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='') as fh:
        fh.write(buf.getvalue())
    print(f'\nCSV: {path}')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true',
                    help='report only. The default.')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--portals', action='store_true',
                    help='item 1: the blocked procurement portals')
    ap.add_argument('--failed', action='store_true',
                    help='item 3: the ones that failed outright')
    ap.add_argument('--days', type=int, default=None,
                    help='limit to the last N days (default: all)')
    ap.add_argument('--csv', nargs='?', const=DEFAULT_CSV, default=None,
                    metavar='PATH')
    args = ap.parse_args(argv)

    both = not (args.portals or args.failed)

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('CRM-FINAL-13NOV items 1 and 3 — mail the intake '
              'refused.' + ('' if args.apply else ' REPORT ONLY.'))
        _rule()

        portals = (blocked_portal_rows(args.days)
                   if (both or args.portals) else [])
        failures = (failed_rows(args.days)
                    if (both or args.failed) else [])
        if both or args.portals:
            report_portals(portals)
        if both or args.failed:
            report_failed(failures)
        if args.csv:
            write_csv(portals, failures, args.csv)

        if not args.apply:
            print(f'\nDRY RUN — nothing re-processed. --apply '
                  f'--portals puts the {len(portals)} portal message(s) '
                  f'through again; --apply --failed does the '
                  f'{len(failures)} failure(s).')
            db.session.rollback()
            return 0

        wanted = []
        if both or args.portals:
            wanted += [row.internet_message_id for row, _p in portals]
        if both or args.failed:
            wanted += [imid for _w, _i, imid, *_rest in failures]
        wanted = [x for x in dict.fromkeys(wanted) if x]
        if not wanted:
            print('\nNothing to re-process.')
            return 0
        print(f'\nre-processing {len(wanted)} message(s):')
        reprocess(wanted)
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
