"""Is leads@ actually being read, and what happened last. READ ONLY.

CRM-WA-01 §4: "Mail sent to the leads@ mailbox does not create or
attach to leads. Check the mail-ingest job for that mailbox: is it
subscribed / polled, is the sender block-list catching it, are errors
logged? Report the last successful ingest time and the error for the
last failed one."

    .venv/bin/python scripts/mail_ingest_status.py --check
    .venv/bin/python scripts/mail_ingest_status.py --check --days 30
    .venv/bin/python scripts/mail_ingest_status.py --check \
          --from somebody@client.example

FIVE QUESTIONS, ANSWERED FROM THE DATABASE AND THE CONFIGURATION

  1. **which way in is live.** `EMAIL_INGESTION_MODE` decides. In
     `mailbox` mode the Graph webhook is the only live path and the
     poll is retired, so "is the poller running" is the wrong
     question — the right one is whether the subscription exists and
     has not expired, because a lapsed subscription is silent: Graph
     simply stops calling and nothing here errors.

  2. **the last message that became something.** Created, attached or
     reopened, with its time. If that is weeks old on a mailbox that
     receives mail daily, the way in is broken rather than the
     filtering.

  3. **the last error, with its reason.** The `error` outcome and the
     sentence stored with it.

  4. **what the block-list and the filters are refusing.** `blocked`
     and `skipped`, counted by reason, because §4's suspicion is that
     a real enquiry is being dropped by a rule meant for newsletters.
     `--from` answers it for one sender.

  5. **whether anything is being dropped with no trace at all.** The
     one failure §4 is really about: a message the CRM saw and did
     not log looks identical to a message that never arrived.

  6. **what the webhook itself recorded.** Added 11 Nov, and it is
     the question the first five could not answer. Between "Graph
     called us" and "a lead appeared" a message can stop in four
     places — a clientState mismatch, a tenant mismatch, a resource
     naming another mailbox, a failed fetch — and every one of them
     writes an `EmailEvent` row and **no** `MailIngestLog` row. So
     this script reported "nothing seen at all" while Graph was
     calling every few minutes and being refused, and the natural
     reading of that was the opposite of the truth.

IT WRITES NOTHING

No `--apply`. It reads the ingest log, the audit log, the
configuration and (if a token is configured) the Graph subscription
list, and prints. Re-processing one message is a button on
Admin → Mail ingest, which is where a person can see what they are
re-processing.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import app, db                                   # noqa: E402

#: Outcomes that mean the message became something in the CRM.
LANDED = ('created', 'attached', 'reopened')


def _rule(n=68):
    print('─' * n)


# ── 1. the way in ────────────────────────────────────────────────────
def report_path():
    from email_ingest import service as mail_service

    mode = mail_service.current_mode()
    mailbox = mail_service.crm_inbox_email()
    print('\n── how mail gets in ──')
    print(f'  mailbox            : {mailbox or "NOT CONFIGURED"}')
    print(f'  EMAIL_INGESTION_MODE: {mode}')
    if mode == mail_service.MAILBOX:
        print('  the Graph webhook is the live path; the poll is '
              'retired in this mode')
    else:
        print('  !! not mailbox mode — the webhook is not the live '
              'path, so check whatever is')

    print('\n── the Graph subscription ──')
    print('  A lapsed subscription is silent: Graph stops calling and '
          'nothing here errors.')
    try:
        from email_ingest import subscription
        rows = subscription.list_active() or []
    except Exception as exc:                              # noqa: BLE001
        print(f'  could not ask Graph ({type(exc).__name__}: '
              f'{str(exc)[:120]})')
        print('  That is a configuration answer, not a verdict — a '
              'missing token here does not mean the subscription is '
              'missing there.')
        return
    if not rows:
        print('  !! NO ACTIVE SUBSCRIPTION. Nothing is being '
              'delivered to the webhook.')
        print('     scripts/email_ingest.py (subscribe) creates one.')
        return
    now = datetime.utcnow()
    for row in rows:
        expiry = str(row.get('expirationDateTime') or '')
        resource = str(row.get('resource') or '')
        note = ''
        try:
            when = datetime.fromisoformat(
                expiry.replace('Z', '+00:00')).replace(tzinfo=None)
            hours = (when - now).total_seconds() / 3600
            note = (f'  ({hours:.1f} h left)' if hours > 0
                    else '  !! EXPIRED')
        except ValueError:
            pass
        print(f'  {resource}')
        print(f'    expires {expiry}{note}')
        print(f'    notificationUrl {row.get("notificationUrl") or "—"}')


# ── 2 and 3. what happened last ──────────────────────────────────────
def report_last():
    from app.models.ingest_log import MailIngestLog

    print('\n── the last message that became something ──')
    last = (MailIngestLog.query
            .filter(MailIngestLog.outcome.in_(LANDED))
            .order_by(MailIngestLog.created_at.desc())
            .first())
    if last is None:
        print('  !! NEVER. No message has ever been turned into a '
              'lead or attached to one.')
    else:
        age = ''
        if last.created_at:
            days = (datetime.utcnow() - last.created_at).days
            age = f'  ({days} day(s) ago)'
        print(f'  {str(last.created_at)[:19]}{age}')
        print(f'    outcome {last.outcome}'
              + (f' on lead #{last.lead_id}' if last.lead_id else ''))
        print(f'    from    {last.from_addr or "—"}')
        print(f'    subject {(last.subject or "")[:70]!r}')

    print('\n── the last error ──')
    worst = (MailIngestLog.query
             .filter(MailIngestLog.outcome == 'error')
             .order_by(MailIngestLog.created_at.desc())
             .first())
    if worst is None:
        print('  none on record.')
    else:
        print(f'  {str(worst.created_at)[:19]}')
        print(f'    from    {worst.from_addr or "—"}')
        print(f'    subject {(worst.subject or "")[:70]!r}')
        print(f'    reason  {worst.reason or "(none stored)"}')


# ── 4. what is being refused ─────────────────────────────────────────
def report_refusals(days, sender=None):
    from app.models.ingest_log import MailIngestLog

    since = datetime.utcnow() - timedelta(days=days)
    rows = (MailIngestLog.query
            .filter(MailIngestLog.created_at >= since)
            .all())
    print(f'\n── the last {days} day(s): {len(rows)} message(s) seen ──')
    if not rows:
        print('  Nothing at all. Either no mail arrived, or nothing is '
              'delivering it — see the subscription above.')
        return
    counts = Counter(r.outcome for r in rows)
    for outcome, n in counts.most_common():
        print(f'  {n:>6}  {outcome}')

    for outcome in ('blocked', 'skipped'):
        group = [r for r in rows if r.outcome == outcome]
        if not group:
            continue
        print(f'\n  === {outcome} ({len(group)}), by reason')
        for reason, n in Counter((r.reason or '(none stored)')[:70]
                                 for r in group).most_common(12):
            print(f'    {n:>5}  {reason}')
        print('  A real enquiry in this list is §4: a rule meant for '
              'newsletters catching a customer.')

    if sender:
        wanted = sender.strip().lower()
        mine = [r for r in rows
                if wanted in (r.from_addr or '').lower()]
        print(f'\n  === everything from {sender!r} ({len(mine)})')
        if not mine:
            print('    nothing from that sender was seen at all in the '
                  'window. The CRM never received it — look at the '
                  'mailbox and the subscription, not at the filters.')
        for r in mine:
            print(f'    {str(r.created_at)[:19]}  {r.outcome:<9} '
                  f'{(r.subject or "")[:44]!r}')
            if r.reason:
                print(f'       {r.reason[:100]}')


# ── 5. the silent drop ───────────────────────────────────────────────
def report_silence():
    print('\n── is anything dropped with no trace? ──')
    print('  The failure §4 is really about: a message the CRM saw and '
          'did not log looks identical to one that never arrived.')
    import ast

    paths = ('email_ingest/single_message.py', 'email_ingest/pipeline.py')
    for path in paths:
        full = os.path.join(os.path.dirname(_HERE), path)
        try:
            tree = ast.parse(open(full).read())
        except OSError:
            print(f'  {path}: could not be read')
            continue
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, 'id', None) == '_record_ingest']
        print(f'  {path}: {len(calls)} logging call site(s)')
    print('  tests/test_unprocessed_mail_log.py walks both files and '
          'fails if any branch returns without writing a row, so this '
          'is a count rather than a promise.')
    print('  Everything logged, whatever the outcome, is on '
          'Admin → Mail ingest, with a re-process button per row.')


# ── 6. the webhook's own log ───────────────────────────────────
def report_webhook(days):
    """Did Graph call us, and what happened to the call?

    `MailIngestLog` starts when a message reaches the classifier.
    `EmailEvent` starts when a notification reaches the route. A
    message refused in between is in the second and not the first,
    which is exactly the state this script used to read as silence.
    """
    from datetime import timedelta

    print('\n── the webhook itself ──')
    try:
        from app import EmailEvent
        since = datetime.utcnow() - timedelta(days=days)
        rows = (EmailEvent.query
                .filter(EmailEvent.received_at >= since)
                .order_by(EmailEvent.id.desc()).all())
    except Exception as exc:                              # noqa: BLE001
        print(f'  could not read EmailEvent ({type(exc).__name__}).')
        return
    if not rows:
        print(f'  !! NOTHING in {days} day(s). Graph never called the '
              f'webhook. Look at the subscription above and at the '
              f'notificationUrl it carries — not at the classifier.')
        return
    print(f'  {len(rows)} notification(s) in {days} day(s):')
    for status, n in Counter((r.status or '?') for r in rows).most_common():
        print(f'    {n:>6}  {status}')
    last = rows[0]
    print(f'  most recent: {str(last.received_at)[:19]}  '
          f'{last.status or "?"}  {(last.reason or "")[:60]}')
    refused = [r for r in rows if (r.status or '') in ('rejected', 'failed')]
    if refused:
        print(f'\n  !! {len(refused)} refused or failed. These never '
              f'reach the ingest log, so the counts above are blind to '
              f'them:')
        for reason, n in Counter(
                (r.reason or '(none stored)')[:70] for r in refused
        ).most_common(8):
            print(f'    {n:>5}  {reason}')
        print('  "clientState mismatch" means EMAIL_WEBHOOK_SECRET is '
              'not the secret the live subscription was created with: '
              'recreate the subscription, or put the old secret back.')
        print('  "wrong mailbox" means the notification named a mailbox '
              'other than the sanctioned one and the scoped fetch '
              '404d — the lockdown doing its job.')
    landed = [r for r in rows if (r.status or '') in ('processed',
                                                     'lead_created')]
    if rows and not landed:
        print('\n  !! Graph is calling and NOTHING has been processed. '
              'The break is between the route and the classifier, '
              'which is where the reasons above point.')
    print('  scripts/2026_11_11_mail_catchup.py --check asks Graph what '
          'it holds and lists what the CRM never processed.')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true', required=True,
                    help='required: this script only ever reads')
    ap.add_argument('--days', type=int, default=14,
                    help='the window for the counts (default 14)')
    ap.add_argument('--from', dest='sender', default=None,
                    metavar='ADDRESS',
                    help='also show everything seen from one sender — '
                         'how to answer "our customer wrote and '
                         'nothing happened"')
    args = ap.parse_args(argv)

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('CRM-WA-01 §4 — is leads@ being read. READ ONLY.')
        _rule()
        report_path()
        report_last()
        report_refusals(args.days, args.sender)
        report_webhook(args.days)
        report_silence()
        print()
        db.session.rollback()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
