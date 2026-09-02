#!/usr/bin/env python
"""
v2026-08-31 — one-off: create / renew / list the Microsoft Graph
subscription that pushes messages from CRM_INBOX_EMAIL (leads@procamgroup.in)
into /api/email/webhook.

Usage:
    python scripts/subscribe_leads_mailbox.py            # list active subs
    python scripts/subscribe_leads_mailbox.py --create   # create a new sub
    python scripts/subscribe_leads_mailbox.py --renew <sub_id>
    python scripts/subscribe_leads_mailbox.py --enforce  # daily-safe:
                                                          #  1. delete any sub
                                                          #     scoped to a
                                                          #     different
                                                          #     mailbox
                                                          #  2. renew or create
                                                          #     the one for
                                                          #     leads@procamgroup.in

Graph subscriptions expire every ~3 days; a cron / systemd timer should
call --renew nightly. This script prints the subscription details on
success and a clear error on failure so you can eyeball what happened.
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# v2026-09-02 — load .env before anything reads os.environ. Under systemd
# the unit's EnvironmentFile supplies these, but a manual run gets nothing,
# and subscription.create() then dies with "CRM_INBOX_EMAIL env var is
# required" *after* an --enforce has already deleted subscriptions. Loading
# it here makes a hand-run behave identically to the timer.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
except ImportError:                                              # pragma: no cover
    pass

from email_ingest import subscription as _sub
from email_ingest import service as _mail


def _preflight() -> None:
    """Verify every env var the create path needs, BEFORE we delete or
    modify anything. An enforce run that deletes a working subscription and
    then cannot create its replacement takes ingestion down completely."""
    missing = [v for v in ('CRM_INBOX_EMAIL', 'EMAIL_WEBHOOK_URL',
                           'EMAIL_WEBHOOK_SECRET', 'MS_TENANT_ID',
                           'MS_CLIENT_ID', 'MS_CLIENT_SECRET')
               if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            'Refusing to run: missing env var(s) ' + ', '.join(missing) +
            '. Without these a subscription could be deleted and not '
            'recreated, which stops ingestion entirely.')


def _enforce():
    """Reconcile Graph subscriptions to a single sanctioned one.

    Deletes any subscription whose resource is not scoped to
    CRM_INBOX_EMAIL. Then, for the surviving sanctioned sub, renews it if
    present or creates a fresh one if not. Safe to run daily via systemd.
    """
    _preflight()
    mailbox = (_mail.crm_inbox_email() or '').strip().lower()
    if not mailbox:
        raise RuntimeError('CRM_INBOX_EMAIL not set — cannot enforce.')

    # v2026-09-02 — identify the mailbox the same way the webhook does:
    # Graph may name it by UPN or by directory object id. A substring match
    # on the UPN alone is what broke the webhook, and here the failure mode
    # is worse — this function DELETES what it cannot identify, so a missed
    # match would destroy the live subscription and silently stop ingestion.
    from email_ingest.graph_client import GraphClient
    from email_ingest.webhook import (_resource_user_segment,
                                      _mailbox_identifiers)
    graph = GraphClient()
    known = _mailbox_identifiers(graph, mailbox)
    # Did we actually resolve the object id, or only have the UPN? If only
    # the UPN, we cannot safely call anything "rogue" — so we don't delete.
    confident = len(known) > 1
    if not confident:
        print('  ! could not resolve the mailbox object id — will keep '
              'unrecognised subscriptions rather than risk deleting the '
              'live one.')

    subs = _sub.list_active()
    print(f'\nFound {len(subs)} active subscription(s).')

    ours_id = None
    for s in subs:
        resource = s.get('resource') or ''
        seg = _resource_user_segment(resource)
        is_ours = bool(seg and seg in known)
        if is_ours:
            if ours_id is None:
                ours_id = s['id']
                print(f'  ✓ keep {s["id"]} — {resource}')
            else:
                # Duplicate sub for the same mailbox — delete extras so we
                # don't get duplicate leads.
                print(f'  x delete duplicate {s["id"]} — {resource}')
                try:
                    _sub.delete(s['id'])
                except Exception as e:
                    print(f'    !! delete failed: {e}')
        elif confident and seg:
            print(f'  x delete rogue {s["id"]} — {resource}')
            try:
                _sub.delete(s['id'])
            except Exception as e:
                print(f'    !! delete failed: {e}')
        else:
            print(f'  ? keeping unidentified {s["id"]} — {resource} '
                  f'(not deleting: cannot prove it is not ours)')

    if ours_id:
        print(f'\nRenewing {ours_id}…')
        res = _sub.renew(ours_id)
        # Persist the id so a later --renew invocation still knows it.
        try:
            with open('/var/www/procam-crm/.leads_subscription_id', 'w') as f:
                f.write(ours_id + '\n')
        except Exception:
            pass
        return res

    print('\nNo sanctioned subscription found. Creating one…')
    res = _sub.create()
    try:
        with open('/var/www/procam-crm/.leads_subscription_id', 'w') as f:
            f.write(res.get('id', '') + '\n')
    except Exception:
        pass
    return res


def main():
    args = sys.argv[1:]
    print(json.dumps({
        'mode': _mail.current_mode(),
        'mailbox': _mail.crm_inbox_email(),
    }, indent=2))

    if not args:
        print('\nActive subscriptions:')
        try:
            print(json.dumps(_sub.list_active(), indent=2, default=str))
        except Exception as e:
            print(f'!! list_active failed: {e}')
            sys.exit(1)
        return

    if args[0] == '--create':
        print('\nCreating subscription…')
        try:
            res = _sub.create()
            print(json.dumps(res, indent=2, default=str))
            print('\n▶ Done. Forward a test email to '
                  + (_mail.crm_inbox_email() or 'the mailbox')
                  + ' and it should land in the CRM within 30-60 seconds.')
        except Exception as e:
            print(f'!! create failed: {e}')
            sys.exit(1)
        return

    if args[0] == '--renew' and len(args) >= 2:
        sub_id = args[1]
        try:
            res = _sub.renew(sub_id)
            print(json.dumps(res, indent=2, default=str))
        except Exception as e:
            print(f'!! renew failed: {e}')
            sys.exit(1)
        return

    if args[0] == '--enforce':
        try:
            res = _enforce()
            print(json.dumps(res, indent=2, default=str))
        except Exception as e:
            print(f'!! enforce failed: {e}')
            sys.exit(1)
        return

    print('usage: subscribe_leads_mailbox.py '
          '[--create | --renew <sub_id> | --enforce]')
    sys.exit(1)


if __name__ == '__main__':
    main()
