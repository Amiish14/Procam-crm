"""Which mailboxes are posting to our webhook, and stopping the ones that
should not be. DRY RUN BY DEFAULT.

CRM-FINAL-13NOV follow-up item 4. In fourteen days the webhook
rejected 67 notifications with "wrong mailbox: Users/9a0b6a4e-...":
a Graph subscription for a DIFFERENT mailbox is still posting to our
notificationUrl, and the lockdown is refusing every one.

    .venv/bin/python scripts/2026_11_14_graph_subscriptions.py --check
    .venv/bin/python scripts/2026_11_14_graph_subscriptions.py --apply
    .venv/bin/python scripts/2026_11_14_graph_subscriptions.py \
          --apply --id <SUBSCRIPTION-ID>

NOTHING WAS LOST, AND THAT IS WHY IT LASTED

The lockdown worked: a notification naming another mailbox is
rejected, and the scoped fetch behind it would 404 anyway, so no
other mailbox's mail has ever reached this CRM. What it costs is
noise -- 67 rejections in a fortnight, each one an `EmailEvent` row
reading like a failure -- and the real failures are harder to see
among them. That is the whole case for cleaning it up, and it is why
this is not urgent.

WHAT IT CONSIDERS A STRANGER

Graph writes the mailbox into `resource` in more than one shape, and
not necessarily the one the subscription was created with: a UPN, or
a directory object id, with or without a leading slash, in varying
case. `email_ingest/webhook.py` already resolves all of those for the
sanctioned mailbox and this asks it, rather than comparing strings
here and disagreeing with the route about which subscription is ours.

A subscription whose resource cannot be read at all is reported and
LEFT. "I could not tell" is not grounds for deleting somebody's
subscription -- it may belong to another application on the same
tenant, and deleting it would break something nobody here can see.

WHAT --apply DOES

Deletes the subscriptions this prints as strangers, one Graph call
each, and prints what is left. It never touches the sanctioned
mailbox's own subscription, and `--id` restricts it to one. Creating
or renewing a subscription is `scripts/email_ingest.py`; this only
ever removes.
"""
from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import app, db                                    # noqa: E402


def _rule(n=72):
    print('─' * n)


def mailbox():
    from email_ingest import service as mail_service
    return mail_service.crm_inbox_email()


def ours(resource, graph, box):
    """Whether this subscription's resource names the sanctioned mailbox.

    Asked of `email_ingest.webhook`, which already knows every shape
    Graph uses: the UPN and the directory object id, cased either way,
    with or without the leading slash. A second comparison written
    here is a second answer, and then the route and the cleanup would
    disagree about which subscription is ours.
    """
    from email_ingest import webhook

    segment = webhook._resource_user_segment(resource or '')
    if not segment:
        return None                      # cannot tell
    try:
        known = webhook._mailbox_identifiers(graph, box)
    except Exception:                                     # noqa: BLE001
        return None
    return segment in known


def survey():
    from email_ingest import subscription
    from email_ingest.graph_client import GraphClient

    box = mailbox()
    print(f'  sanctioned mailbox : {box or "NOT CONFIGURED"}')
    if not box:
        print('  !! CRM_INBOX_EMAIL is not set. Without it there is no '
              '"ours" to compare against, and nothing here would be '
              'safe to delete.')
        raise SystemExit(2)
    try:
        rows = subscription.list_active() or []
    except Exception as exc:                              # noqa: BLE001
        print(f'  !! could not ask Graph for the subscriptions: '
              f'{type(exc).__name__}: {str(exc)[:160]}')
        print('     That is a configuration answer rather than a '
              'verdict. Nothing was changed.')
        raise SystemExit(2)

    graph = GraphClient()
    mine, strangers, unknown = [], [], []
    for row in rows:
        verdict = ours(row.get('resource'), graph, box)
        if verdict is True:
            mine.append(row)
        elif verdict is False:
            strangers.append(row)
        else:
            unknown.append(row)
    return mine, strangers, unknown


def _show(row, note=''):
    print(f'    id            {row.get("id")}')
    print(f'    resource      {(row.get("resource") or "")[:90]}')
    print(f'    notificationUrl {(row.get("notificationUrl") or "")[:80]}')
    print(f'    expires       {row.get("expirationDateTime") or "—"}')
    print(f'    changeType    {row.get("changeType") or "—"}')
    if note:
        print(f'    {note}')


def report(mine, strangers, unknown):
    print(f'\n══ {len(mine)} ours, {len(strangers)} not ours, '
          f'{len(unknown)} undecidable ══')

    print('\n  ── the sanctioned mailbox\'s own ──')
    if not mine:
        print('    !! NONE. Nothing is delivering mail to this CRM at '
              'all. Create one with scripts/email_ingest.py before '
              'deleting anything here.')
    for row in mine:
        _show(row, 'kept, always')

    print('\n  ── not this mailbox ──')
    if not strangers:
        print('    none. Whatever was posting the 67 rejected '
              'notifications has expired or been removed already — '
              'Graph subscriptions last three days unless renewed.')
    for row in strangers:
        _show(row, 'would be DELETED')

    if unknown:
        print('\n  ── cannot tell ──')
        for row in unknown:
            _show(row, 'LEFT: the resource could not be resolved to a '
                       'mailbox. It may belong to another application '
                       'on this tenant.')


def apply(strangers, only_id=None):
    from email_ingest import subscription

    wanted = [row for row in strangers
              if not only_id or row.get('id') == only_id]
    if only_id and not wanted:
        print(f'\n  !! {only_id} is not in the list of strangers above. '
              f'Nothing deleted — this script will not delete a '
              f'subscription it has not just judged to be somebody '
              f'else\'s.')
        raise SystemExit(2)
    if not wanted:
        print('\nNothing to delete.')
        return 0
    gone = 0
    for row in wanted:
        try:
            subscription.delete(row.get('id'))
            print(f'  deleted {row.get("id")}  '
                  f'({(row.get("resource") or "")[:60]})')
            gone += 1
        except Exception as exc:                          # noqa: BLE001
            print(f'  !! {row.get("id")}: {type(exc).__name__}: '
                  f'{str(exc)[:140]}')
    print(f'\n{gone} subscription(s) deleted.')
    print('The webhook should stop recording "wrong mailbox" rejections '
          'within a few minutes. scripts/mail_ingest_status.py --check '
          'counts them.')
    return gone


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true',
                    help='list them. The default.')
    ap.add_argument('--apply', action='store_true',
                    help='delete the ones that are not this mailbox')
    ap.add_argument('--id', default=None, metavar='SUBSCRIPTION-ID',
                    help='with --apply, delete only this one')
    args = ap.parse_args(argv)

    with app.app_context():
        print('CRM-FINAL-13NOV item 4 — Graph subscriptions posting to '
              'this webhook.' + ('' if args.apply else ' REPORT ONLY.'))
        _rule()
        mine, strangers, unknown = survey()
        report(mine, strangers, unknown)
        if not args.apply:
            print(f'\nDRY RUN — nothing deleted. --apply removes the '
                  f'{len(strangers)} subscription(s) marked DELETED '
                  f'above.')
            db.session.rollback()
            return 0
        apply(strangers, only_id=args.id)
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
