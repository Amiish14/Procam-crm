#!/usr/bin/env python
"""
Can the CRM send email? — the one command that answers it.

Microsoft Graph tells us what it granted: a client-credentials access
token lists its application permissions in the `roles` claim. Reading
that claim is a complete answer and sends nothing, so this is safe to
run at any hour, as often as you like, on production.

    .venv/bin/python scripts/check_mail_send.py

    # prove it end to end once IT says the grant is done (SENDS ONE MAIL)
    .venv/bin/python scripts/check_mail_send.py --send-test you@procamgroup.in

Exit status: 0 the CRM can send (and the test message went out),
1 Mail.Send is missing or the send failed, 2 no token could be obtained
so the question could not be asked.

The grant itself is an administrator's job, not the CRM team's:
docs/operations/GRAPH_MAIL_SEND.md is written for them.

No token, secret or password is printed by this script.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
# The repository root goes in front of scripts/, which holds an
# email_ingest.py that would otherwise shadow the email_ingest package.
sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, '.env'))
except ImportError:                                  # pragma: no cover
    pass

# The decoder, the role list and the wording of the verdict live in the
# preflight, so this CLI, the preflight and the ops page cannot drift.
import production_preflight as pf                    # noqa: E402

OK_SEND, CANNOT_SEND, NO_TOKEN = 0, 1, 2


def fetch_token():
    """('ok', token) | ('missing'|'unreachable'|'refused', why).

    A seam: the tests replace this so nothing in the suite goes near
    login.microsoftonline.com.
    """
    return pf.graph_access_token()


def mailbox():
    """The mailbox the CRM sends as."""
    from email_ingest import notifier
    return notifier.sender() or '(not set)'


def notifications_enabled():
    """NOTIFY_ENABLED. A test send through the notifier obeys it, so a
    refusal here is better than a mysterious 'the send did not succeed'."""
    from email_ingest import notifier
    return notifier.is_enabled()


def _send_test(address, granted_roles):
    """One real message through the production notifier."""
    from email_ingest import notifier
    html = notifier._SHELL.format(
        heading='Mail.Send verification',
        body='<p>If you are reading this, the Microsoft Graph '
             '<strong>Mail.Send</strong> application permission is granted '
             'and the CRM can send email: assignment notifications, '
             'Workbench reminders and the scheduled reports will be '
             'delivered.</p>'
             '<p style="color:#6B6762;font-size:12.5px;">Sent by '
             'scripts/check_mail_send.py. Granted permissions on the '
             'token: ' + ', '.join(granted_roles) + '.</p>')
    return notifier.send(address, '[TEST] Procam CRM can send email', html)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Report which Microsoft Graph application permissions '
                    'the CRM holds, and whether it can send email.')
    ap.add_argument('--send-test', metavar='ADDRESS',
                    help='after checking, send one test message to this '
                         'address through the CRM\'s own notifier '
                         '(refused when Mail.Send is not granted)')
    args = ap.parse_args(argv)

    state, info = fetch_token()
    if state != 'ok':
        why = {
            'missing': f'Graph credentials are not configured: {info} not '
                       f'set in .env',
            'unreachable': f'login.microsoftonline.com did not answer: '
                           f'{info}',
            'refused': f'Microsoft refused the credentials: {info}',
        }[state]
        print(f'CANNOT TELL — {why}')
        print('Nothing about Mail.Send can be established until a token is '
              'obtained. Check MS_TENANT_ID, MS_CLIENT_ID and '
              'MS_CLIENT_SECRET, then run this again.')
        return NO_TOKEN

    claims = pf.decode_jwt_claims(info)
    roles = pf.token_roles(info)
    granted = pf.MAIL_SEND in roles

    print('app id   :', claims.get('appid') or claims.get('azp') or '?')
    print('tenant   :', claims.get('tid') or '?')
    print('mailbox  :', mailbox())
    print()
    print(f'--- application permissions on this token ({len(roles)}) ---')
    for role in roles:
        print('  ' + role)
    if not roles:
        print('  (none — nothing has been admin-consented for this app)')

    print()
    if granted:
        print('CAN SEND — Mail.Send is granted. Assignment emails, '
              'Workbench reminders and the five scheduled reports will go '
              'out.')
        print('A 403 from here on would be an Exchange application access '
              'policy excluding the mailbox, not a missing permission: '
              'see docs/operations/GRAPH_MAIL_SEND.md §5.')
    else:
        _granted, detail = pf.mail_send_verdict(roles)
        print('CANNOT SEND — Mail.Send is ' + detail)
        print('Give docs/operations/GRAPH_MAIL_SEND.md to whoever '
              'administers Microsoft 365 for Procam; it is written for '
              'them and takes about ten minutes.')

    if not args.send_test:
        return OK_SEND if granted else CANNOT_SEND

    print()
    if not granted:
        # Refuse rather than send: the attempt would only produce the
        # same 403 that is already explained above, and the operator
        # would be left wondering which of the two faults they hit.
        print(f'--send-test refused: Mail.Send is not granted, so a test '
              f'message to {args.send_test} would fail with HTTP 403 '
              f'ErrorAccessDenied. Have the permission granted and '
              f'admin-consented first, then run this again.')
        return CANNOT_SEND

    if not notifications_enabled():
        print('--send-test refused: NOTIFY_ENABLED is off, so the notifier '
              'sends nothing. That is a CRM setting, not a permission: the '
              'verdict on Mail.Send above still stands.')
        return CANNOT_SEND

    print(f'sending one test message to {args.send_test} ...')
    if _send_test(args.send_test, roles):
        print('SENT — the grant works end to end. Check the inbox, and the '
              'Sent Items of the leads mailbox.')
        return OK_SEND
    print('FAILED — the permission is granted but the send did not '
          'succeed. The CRM log holds the Graph response; the usual cause '
          'is an Exchange application access policy that excludes the '
          'mailbox (docs/operations/GRAPH_MAIL_SEND.md §5).')
    return CANNOT_SEND


if __name__ == '__main__':
    raise SystemExit(main())
