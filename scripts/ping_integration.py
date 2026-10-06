"""
Prove the TMS's bearer token works, without anybody reading it.

Reads CRM_INTEGRATION_TOKENS from the CRM's own environment file,
takes the token for one caller, and calls /ping with it. The token is
used and never printed — not in the output, not in a shell history,
not in a process list, which is why this is a script rather than a
curl command with the value pasted into it.

    .venv/bin/python scripts/ping_integration.py
    .venv/bin/python scripts/ping_integration.py --caller procam-tms \
        --base http://127.0.0.1:8002/api/integration/v1
"""
import argparse
import json
import os
import sys

DEFAULT_BASE = 'http://127.0.0.1:8002/api/integration/v1'


def _token_for(env_path, caller):
    try:
        with open(env_path, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith('CRM_INTEGRATION_TOKENS='):
                    continue
                raw = line.partition('=')[2].strip()
                if len(raw) >= 2 and raw[0] == raw[-1] in ('"', "'"):
                    raw = raw[1:-1]
                for part in raw.split(','):
                    name, _, token = part.strip().partition(':')
                    if name.strip() == caller and token.strip():
                        return token.strip()
    except OSError as exc:
        print(f'could not read {env_path}: {exc}')
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--env-file', default='/var/www/procam-crm/.env')
    ap.add_argument('--caller', default='procam-tms')
    ap.add_argument('--base', default=DEFAULT_BASE)
    ap.add_argument('--timeout', type=int, default=15)
    args = ap.parse_args()

    token = _token_for(args.env_file, args.caller)
    if not token:
        print(f'no token for caller {args.caller!r} in '
              f'CRM_INTEGRATION_TOKENS.')
        return 2
    print(f'caller   {args.caller}')
    print(f'token    found, {len(token)} characters (not shown)')
    print(f'base     {args.base}')

    import requests
    url = args.base.rstrip('/') + '/ping'
    try:
        resp = requests.get(url, timeout=args.timeout, headers={
            'Authorization': f'Bearer {token}'})
    except Exception as exc:                                  # noqa: BLE001
        print(f'\ncould not reach {url}: {exc}')
        return 1
    finally:
        del token

    print(f'\nHTTP {resp.status_code}')
    try:
        body = resp.json()
    except ValueError:
        print(resp.text[:400])
        return 1
    print(json.dumps(body, indent=2))

    if resp.status_code == 200 and body.get('caller') == args.caller:
        print(f'\nThe CRM recognises this token as {args.caller}.')
        return 0
    print('\nThe CRM did not accept it as that caller.')
    return 1


if __name__ == '__main__':
    sys.exit(main())
