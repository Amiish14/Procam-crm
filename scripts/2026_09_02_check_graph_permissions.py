#!/usr/bin/env python
"""
v2026-09-02 — Show which Microsoft Graph permissions the CRM's token
actually carries, and probe what it can do with the leads mailbox.

Answers the question "is Mail.Send missing, or is it granted but blocked?"
The two look similar from the outside but need completely different fixes:

  * `roles` has no Mail.Send        -> IT must grant + admin-consent it
  * `roles` has Mail.Send, still 403 -> an Exchange application access
                                        policy is excluding this mailbox,
                                        or the mailbox itself is the problem

Usage:
    python scripts/2026_09_02_check_graph_permissions.py
"""
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
except ImportError:                                              # pragma: no cover
    pass

from email_ingest import service as mail_service              # noqa: E402
from email_ingest.graph_client import GraphClient             # noqa: E402


def decode_claims(token: str) -> dict:
    """Decode a JWT payload without verifying — we only want to read it."""
    try:
        payload = token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception as e:                                       # noqa: BLE001
        return {'_decode_error': str(e)}


def main():
    mailbox = mail_service.crm_inbox_email()
    graph = GraphClient()
    token = graph.get_token()
    claims = decode_claims(token)

    print('app id   :', claims.get('appid') or claims.get('azp') or '?')
    print('tenant   :', claims.get('tid') or '?')
    print('audience :', claims.get('aud') or '?')
    print('mailbox  :', mailbox)

    roles = claims.get('roles') or []
    print(f'\n--- application permissions on this token ({len(roles)}) ---')
    for r in sorted(roles):
        print('  ' + r)
    if not roles:
        print('  (none — the app registration has no application permissions,'
              ' or consent was never granted)')

    print('\n--- verdict ---')
    if 'Mail.Send' in roles:
        print('  Mail.Send IS granted.')
        print('  A 403 on sendMail therefore is NOT a missing permission. The')
        print('  usual cause is an Exchange application access policy that')
        print('  excludes this mailbox. Ask IT to run:')
        print(f'    Test-ApplicationAccessPolicy -Identity {mailbox} \\')
        print(f'      -AppId {claims.get("appid", "<app-id>")}')
    else:
        print('  Mail.Send is NOT on the token — this is a missing permission.')
        print('  IT must add Mail.Send (APPLICATION, not Delegated) to the app')
        print('  registration and click "Grant admin consent". Until then no')
        print('  mail can be sent, regardless of code.')

    # Probe a couple of endpoints so the picture is complete.
    print('\n--- live probes ---')
    for label, path in (
        ('read the leads mailbox', f"/users/{mailbox}/messages?$top=1&$select=id"),
        ('read the mailbox object', f"/users/{mailbox}?$select=id,mail"),
    ):
        try:
            resp = graph._request('GET', path)
            print(f'  {label:<26} HTTP {resp.status_code}')
        except Exception as e:                                   # noqa: BLE001
            print(f'  {label:<26} error: {str(e)[:70]}')


if __name__ == '__main__':
    main()
