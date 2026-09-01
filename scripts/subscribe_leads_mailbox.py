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

from email_ingest import subscription as _sub
from email_ingest import service as _mail


def _enforce():
    """Reconcile Graph subscriptions to a single sanctioned one.

    Deletes any subscription whose resource is not scoped to
    CRM_INBOX_EMAIL. Then, for the surviving sanctioned sub, renews it if
    present or creates a fresh one if not. Safe to run daily via systemd.
    """
    mailbox = (_mail.crm_inbox_email() or '').strip().lower()
    if not mailbox:
        raise RuntimeError('CRM_INBOX_EMAIL not set — cannot enforce.')

    subs = _sub.list_active()
    print(f'\nFound {len(subs)} active subscription(s).')

    ours_id = None
    for s in subs:
        resource = (s.get('resource') or '').lower()
        # Match on the address substring, case-insensitive.
        if f'/users/{mailbox}/' in resource:
            if ours_id is None:
                ours_id = s['id']
                print(f'  ✓ keep {s["id"]} — {s.get("resource")}')
            else:
                # Duplicate sub for the same mailbox — delete extras so we
                # don't get duplicate leads.
                print(f'  x delete duplicate {s["id"]} — {s.get("resource")}')
                try:
                    _sub.delete(s['id'])
                except Exception as e:
                    print(f'    !! delete failed: {e}')
        else:
            print(f'  x delete rogue {s["id"]} — {s.get("resource")}')
            try:
                _sub.delete(s['id'])
            except Exception as e:
                print(f'    !! delete failed: {e}')

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
