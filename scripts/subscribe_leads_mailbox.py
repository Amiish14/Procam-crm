#!/usr/bin/env python
"""
v2026-08-31 — one-off: create / renew / list the Microsoft Graph
subscription that pushes messages from CRM_INBOX_EMAIL (leads@procamgroup.in)
into /api/email/webhook.

Usage:
    python scripts/subscribe_leads_mailbox.py            # list active subs
    python scripts/subscribe_leads_mailbox.py --create   # create a new sub
    python scripts/subscribe_leads_mailbox.py --renew <sub_id>

Graph subscriptions expire every ~3 days; a cron / systemd timer should
call --renew nightly. This script prints the subscription details on
success and a clear error on failure so you can eyeball what happened.
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from email_ingest import subscription as _sub
from email_ingest import service as _mail


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

    print('usage: subscribe_leads_mailbox.py [--create | --renew <sub_id>]')
    sys.exit(1)


if __name__ == '__main__':
    main()
