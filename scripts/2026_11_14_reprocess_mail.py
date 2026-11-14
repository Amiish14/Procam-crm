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

They come back differently now, and -- since 13 Nov -- grouped.
`data/portal_senders.txt` wins over the denylist, and a lead is made
per portal EVENT rather than per message: the number the portal prints
in every mail of the set (RFX-, RFQ-, RA-, an Ariba document or event
id) holds the invitation, the reminders, "technical live", "about to
end" and the withdrawal together on one lead, flagged for review and
owned by the RFQ desk, with the buyer read out of the body. A
withdrawal closes that lead as Lost ("withdrawn by buyer"); an "about
to end" sets its submission deadline, unconfirmed. See
`app/services/portal_rfq.py` and `app/services/portal_event.py`.

`--check --portals` prints EVENTS, with the messages counted under
them, and says how many leads would be created. A withdrawal of an
event that has no lead creates nothing -- a lead born Lost is not
something anybody can act on -- and is recorded in the ingest log
with its number instead.

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
#: Outcomes a portal message can carry once it has been put through
#: again. `blocked` alone was right before the first --apply and wrong
#: after it: the row's outcome moves to whatever happened the second
#: time, so asking only for `blocked` loses sight of every message the
#: re-run touched -- which is precisely the set somebody then wants to
#: ask about.
SEEN = ('blocked', 'skipped', 'created', 'attached', 'reopened',
        'error')


def blocked_portal_rows(days=None, outcomes=('blocked',)):
    """Ingest rows whose sender is a procurement portal."""
    from app.models.ingest_log import MailIngestLog
    from app.services import portal_rfq

    q = MailIngestLog.query.filter(MailIngestLog.outcome.in_(outcomes))
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


def group_events(rows):
    """[(key, [(row, portal), ...]), ...] — the EVENTS, not the mails.

    13 Nov, and the reason this script was rewritten. Of the 74
    messages the denylist held back, about 30 are "withdrawn" notices
    and most of the rest reminders: one lead per message would have
    made 74 leads for perhaps a dozen auctions. The number the portal
    prints in every mail of the set is the event
    (`portal_rfq.event_key`), and that is what a lead is made per.

    ONE HONEST LIMIT, SAID IN THE OUTPUT

    The ingest log keeps the subject and the sender, NOT the body. So
    the key here can only be read from the subject, and a message
    whose number is in the body alone is counted as an event of its
    own below. At `--apply` the message is fetched from Graph and the
    body IS read, so such a message may still join another event's
    lead. The event count here is therefore an upper bound on the
    leads, never an under-count, and the print says so.
    """
    from app.services import portal_rfq

    groups = {}
    for row, portal in rows:
        key = portal_rfq.event_key(row.from_addr or '',
                                   row.subject or '', '')
        if not key:
            key = f'(no number in the subject) #{row.id}'
        groups.setdefault(key, []).append((row, portal))
    ordered = sorted(
        groups.items(),
        key=lambda item: min(
            str(r.received_at or r.created_at or '') for r, _p in item[1]))
    return ordered


def _lead_for(key):
    """The lead that already carries this event number, if any."""
    from app import Lead

    if key.startswith('('):
        return None
    try:
        return (Lead.query
                .filter(Lead.portal_key == key,
                        Lead.is_archived.isnot(True))
                .order_by(Lead.id.asc()).first())
    except Exception:                                     # noqa: BLE001
        return None


def _kinds(messages):
    from app.services import portal_rfq

    return Counter(portal_rfq.notice_kind(row.subject or '', '')
                   for row, _portal in messages)


def plan(groups):
    """What each event would do. [(key, messages, verdict, note)]."""
    from app.services import portal_rfq

    out = []
    for key, messages in groups:
        kinds = _kinds(messages)
        held = _lead_for(key)
        unknown = key.startswith('(')
        if held is not None:
            verdict = 'attach'
            note = (f'lead #{held.id} already carries {key} — all '
                    f'{len(messages)} message(s) go on its trail')
        elif (set(kinds) == {portal_rfq.WITHDRAWN}):
            verdict = 'no lead'
            note = ('every message about this event is a withdrawal '
                    'and no lead exists — recorded in the ingest log, '
                    'no lead created')
        else:
            verdict = 'create'
            others = len(messages) - 1
            note = (f'one lead, from the earliest message; the other '
                    f'{others} '
                    + ('message goes' if others == 1 else 'messages go')
                    + ' on its trail'
                    if others else 'one lead')
            if kinds.get(portal_rfq.WITHDRAWN):
                note += '; then closed as Lost (withdrawn by buyer)'
            elif kinds.get(portal_rfq.ENDING):
                note += '; the deadline read off the "about to end" notice'
        if unknown:
            note += (' — no number in the subject, so this may still '
                     'join another event once the body is read')
        out.append((key, messages, verdict, note))
    return out


def report_outcomes(rows):
    """What the ingest log says happened to each of these messages.

    13 Nov. The first --apply reported "9 created, 65 skipped" and
    nothing else: the loop printed the status and the lead id and
    threw the reason away, so sixty-five messages had a reason stored
    in the ingest log that nobody could see. The reason is the whole
    diagnosis -- "previously purged", "already classified", "joined
    lead 12644" and "withdrawn, no lead" are four completely
    different situations and they all print as `skipped`.

    Read-only, and read from the log rather than from a re-run: the
    rows already hold the answer.
    """
    from app.services import portal_rfq

    print(f'\n══ what happened to each of the {len(rows)} message(s) '
          f'══')
    if not rows:
        print('  none on record.')
        return
    by_reason = Counter(
        (row.outcome, (row.reason or '(no reason stored)')[:72])
        for row, _portal in rows)
    print(f'\n  {"n":>4}  {"outcome":<9} reason')
    for (outcome, reason), n in by_reason.most_common():
        print(f'  {n:>4}  {outcome:<9} {reason}')

    no_lead = [row for row, _p in rows
               if not row.lead_id and row.outcome != 'created']
    print(f'\n  {len(no_lead)} message(s) carry no lead. Each one, '
          f'with its reason:')
    for row in no_lead:
        key = portal_rfq.event_key(row.from_addr or '',
                                   row.subject or '', '')
        print(f'    {str(row.received_at or row.created_at or "")[:16]}'
              f'  {row.outcome:<9} {key or "(no number)":<26} '
              f'{(row.subject or "")[:46]!r}')
        print(f'      → {(row.reason or "(no reason stored)")[:150]}')
    if no_lead:
        print('\n  A reason of "previously purged as irrelevant" is '
              'the one `force` does NOT override:')
        print('  --apply --portals --include-purged re-processes '
              'those, and records that the purge was overridden.')


def report_portals(rows):
    """The events, with the messages counted under them."""
    from app.services import portal_rfq

    print(f'\n══ {len(rows)} blocked message(s) from a procurement '
          f'portal ══')
    if not rows:
        print('  none on record.')
        return []
    by_portal = Counter(portal for _row, portal in rows)
    for portal, n in by_portal.most_common():
        print(f'  {n:>5}  {portal}')

    groups = group_events(rows)
    planned = plan(groups)
    print(f'\n══ {len(planned)} portal EVENT(S) in those '
          f'{len(rows)} message(s) ══')
    print('  One lead per event, not per message. The later notices '
          '(reminder, technical live,')
    print('  about to end, withdrawn) go on that lead\'s mail trail.\n')
    print(f'  {"event":<26} {"msgs":>4}  {"does":<8} what, and the '
          f'kinds in it')
    for key, messages, verdict, note in planned:
        kinds = _kinds(messages)
        spread = '+'.join(f'{n} {kind}' for kind, n
                          in kinds.most_common())
        first = min(str(r.received_at or r.created_at or '')[:10]
                    for r, _p in messages)
        last = max(str(r.received_at or r.created_at or '')[:10]
                   for r, _p in messages)
        when = first if first == last else f'{first}→{last}'
        print(f'  {key[:26]:<26} {len(messages):>4}  {verdict:<8} '
              f'{when}  {spread}')
        print(f'  {"":<26} {"":>4}  {note}')

    creates = [p for p in planned if p[2] == 'create']
    attaches = [p for p in planned if p[2] == 'attach']
    refused = [p for p in planned if p[2] == 'no lead']
    print(f'\n  ── what --apply --portals would do ──')
    print(f'  {len(creates):>5}  lead(s) would be CREATED — one per '
          f'event, flagged for review, owned by the RFQ desk')
    print(f'  {sum(len(m) for _k, m, _v, _n in creates) - len(creates):>5}'
          f'  further message(s) would go on those leads\' trails')
    print(f'  {len(attaches):>5}  event(s) already have a lead; their '
          f'{sum(len(m) for _k, m, _v, _n in attaches)} message(s) '
          f'attach to it')
    print(f'  {len(refused):>5}  event(s) are withdrawals of events '
          f'with no lead: recorded, NO lead created')
    closes = sum(1 for _k, m, v, _n in planned
                 if v == 'create'
                 and _kinds(m).get(portal_rfq.WITHDRAWN))
    print(f'  {closes:>5}  of the created leads would then be closed '
          f'as Lost ("{portal_rfq.LOST_REASON}")')
    print(f'\n  So: {len(rows)} messages, {len(planned)} events, at '
          f'most {len(creates)} new leads — and fewer if any of the '
          f'subject-less ones join an event once its body is read.')

    with_buyer = 0
    for row, _portal in rows:
        buyer, _how = portal_rfq.buyer_from(row.subject or '', '')
        if buyer:
            with_buyer += 1
    print(f'\n  the buyer can be read from the SUBJECT alone on '
          f'{with_buyer} of {len(rows)}. The rest will be read from the '
          f'body when the message is fetched again, and anything still '
          f'unnamed lands as "To review", flagged — not dropped.')
    return planned


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
def apply_order(planned):
    """The message ids to re-process, in the order that matters.

    EVENT BY EVENT, and inside an event oldest first. The order is the
    whole of "one lead per event": the first message makes the lead
    and carries the number, and every later notice finds that lead and
    joins it. Shuffle it and a reminder makes the lead while the
    invitation -- the one message that describes the cargo -- becomes
    a line on its trail.

    A function of its own so the order can be asserted without a
    Graph token, which is the half of an --apply that cannot be tested
    any other way.
    """
    out = []
    for _key, messages, _verdict, _note in planned:
        for row, _portal in sorted(
                messages,
                key=lambda item: (str(item[0].received_at
                                      or item[0].created_at or ''),
                                  item[0].id)):
            if row.internet_message_id:
                out.append(row.internet_message_id)
    return out



def reprocess(imids, *, include_purged=False):
    """Put each message id through the webhook's own function.

    `include_purged` carries one decision and no others: a message a
    person deleted as irrelevant is normally left alone whatever
    `force` says, and this is how an operator says "not these --
    these are RFQs". Every override writes an audit row.
    """
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
            out = process_single_message(graph, box, msg, force=True,
                                         override_purge=include_purged)
            status = (out or {}).get('status') or 'no status'
            lead_id = (out or {}).get('lead_id')
            why = (out or {}).get('reason') or ''
        except Exception as exc:                          # noqa: BLE001
            status, lead_id = f'raised {type(exc).__name__}', None
            why = str(exc)[:120]
        counts[f'{status}: {why[:50]}' if status == 'skipped' and why
               else status] += 1
        print(f'  {status:<12} {"lead #" + str(lead_id) if lead_id else "":<10}'
              f' {(msg.get("subject") or "")[:46]!r}')
        # THE REASON, which the first run threw away. "9 created, 65
        # skipped" with no reasons is not a report, it is a riddle.
        if why:
            print(f'      → {why[:140]}')
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
    from app.services import portal_rfq

    out.writerow(['kind', 'event', 'notice', 'when', 'sender',
                  'subject', 'message_id', 'reason'])
    for row, portal in portals:
        subject = row.subject or ''
        out.writerow(['portal:' + portal,
                      portal_rfq.event_key(row.from_addr or '', subject,
                                           '') or '(not in the subject)',
                      portal_rfq.notice_kind(subject, ''),
                      str(row.received_at or row.created_at or '')[:19],
                      row.from_addr or '', subject[:120],
                      row.internet_message_id or '', row.reason or ''])
    for where, _id, imid, when, sender, subject, reason in failures:
        out.writerow(['failed:' + where, '', '', str(when or '')[:19],
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
    ap.add_argument('--why', action='store_true',
                    help='what the ingest log says happened to each '
                         'portal message, with its reason. Read-only '
                         'whatever else is passed.')
    ap.add_argument('--missing', action='store_true',
                    help='only the portal messages that still have no '
                         'lead — after a run that left some behind')
    ap.add_argument('--include-purged', action='store_true',
                    help='also re-process messages somebody purged as '
                         'irrelevant. Each override is audited.')
    ap.add_argument('--days', type=int, default=None,
                    help='limit to the last N days (default: all)')
    ap.add_argument('--csv', nargs='?', const=DEFAULT_CSV, default=None,
                    metavar='PATH')
    args = ap.parse_args(argv)

    both = not (args.portals or args.failed or args.why)
    wants_portals = both or args.portals or args.why or args.missing

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('CRM-FINAL-13NOV items 1 and 3 — mail the intake '
              'refused.' + ('' if args.apply else ' REPORT ONLY.'))
        _rule()

        # Every outcome, not only `blocked`: after a run the rows say
        # what happened the second time, and `blocked` alone would
        # hide exactly the messages somebody is asking about.
        portals = (blocked_portal_rows(args.days, outcomes=SEEN)
                   if wants_portals else [])
        if args.missing:
            before = len(portals)
            portals = [(row, portal) for row, portal in portals
                       if not row.lead_id and row.outcome != 'created']
            print(f'\n--missing: {len(portals)} of {before} portal '
                  f'message(s) still carry no lead.')
        failures = (failed_rows(args.days)
                    if (both or args.failed) else [])
        planned = []
        if args.why:
            report_outcomes(portals)
        if both or args.portals or args.missing:
            planned = report_portals(portals) or []
        if both or args.failed:
            report_failed(failures)
        if args.csv:
            write_csv(portals, failures, args.csv)

        if not args.apply:
            _leads = sum(1 for _k, _m, v, _n in planned if v == 'create')
            print(f'\nDRY RUN — nothing re-processed. --apply '
                  f'--portals puts the {len(portals)} portal message(s) '
                  f'through again as {len(planned)} event(s) '
                  f'({_leads} new lead(s) at most); --apply --failed '
                  f'does the {len(failures)} failure(s).')
            db.session.rollback()
            return 0

        if args.why and not (args.portals or args.missing or args.failed):
            print('\n--why is a report. Nothing was re-processed.')
            db.session.rollback()
            return 0

        wanted = []
        if both or args.portals or args.missing:
            for key, messages, verdict, _note in planned:
                print(f'  {key}: {len(messages)} message(s), {verdict}')
            wanted += apply_order(planned)
        if both or args.failed:
            wanted += [imid for _w, _i, imid, *_rest in failures]
        wanted = [x for x in dict.fromkeys(wanted) if x]
        if not wanted:
            print('\nNothing to re-process.')
            return 0
        print(f'\nre-processing {len(wanted)} message(s), oldest '
              f'first within each event'
              + (', INCLUDING the purged ones (each override is '
                 'audited)' if args.include_purged else '') + ':')
        reprocess(wanted, include_purged=args.include_purged)
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
