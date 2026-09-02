#!/usr/bin/env python
"""
v2026-09-02 — Verify the CRM can send email, before anything depends on it.

Sends one test message through Microsoft Graph from the leads mailbox. The
most likely failure is HTTP 403: the Azure app registration has Mail.Read
(for ingestion) but not **Mail.Send**, which is a separate application
permission needing admin consent.

Usage:
    python scripts/2026_09_02_test_notification.py --to you@procamgroup.in
    python scripts/2026_09_02_test_notification.py --to you@x.com --lead 10131
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
except ImportError:                                              # pragma: no cover
    pass

from app import app, Lead, Employee                           # noqa: E402
from email_ingest import notifier                             # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--to', required=True, help='recipient address')
    ap.add_argument('--lead', type=int,
                    help='render a real lead-assigned email for this lead id')
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')

    print('enabled  :', notifier.is_enabled())
    print('from     :', notifier.sender() or '(not set)')
    print('base url :', notifier.base_url() or '(not set — links will be relative)')
    print('to       :', args.to)

    with app.app_context():
        if args.lead:
            lead = Lead.query.get(args.lead)
            if not lead:
                sys.exit(f'no lead with id {args.lead}')
            print('lead     :', lead.company)
            print('link     :', notifier.lead_link(lead.id))
            html = notifier.lead_assigned_html(lead, assigned_by='Test run')
            subject = f'[TEST] New lead assigned: {(lead.company or "")[:60]}'
        else:
            html = notifier._SHELL.format(
                heading='Notification test',
                body='<p>If you are reading this, the CRM can send email '
                     'through Microsoft Graph.</p>')
            subject = '[TEST] Procam CRM notification check'

        ok = notifier.send(args.to, subject, html)

    print()
    if ok:
        print('SENT — Mail.Send is granted and notifications will work.')
    else:
        print('FAILED — see the error above.')
        print('If it was HTTP 403, ask IT to grant the Azure app registration')
        print('the Mail.Send APPLICATION permission and give admin consent.')
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
