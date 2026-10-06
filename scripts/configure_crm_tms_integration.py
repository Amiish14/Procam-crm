"""
Issue the CRM ↔ TMS credentials, without anybody seeing them.

Two secrets, two applications, and they have to match across the
pair or the integration fails closed at the first call:

    integration bearer   CRM  CRM_INTEGRATION_TOKENS = "procam-tms:<s>"
                         TMS  CRM_INTEGRATION_TOKEN  = "<s>"
    webhook secret       CRM  TMS_WEBHOOK_TOKEN      = "<w>"
                         TMS  TMS_WEBHOOK_TOKEN      = "<w>"

Note the shapes differ on the first pair. The CRM holds a *registry*
— `name:token`, comma separated, because several systems may call it
and the name is what appears in the integration log. The TMS holds
only its own token, because that is all it sends. Writing the CRM's
`name:token` form into the TMS would authenticate as nothing.

The two secrets are independent on purpose: one lets the TMS call the
CRM, the other lets the CRM call the TMS. Reusing one value for both
would mean a leak in either direction compromises both.

Nothing is printed. Not at generation, not at write, not at verify —
the output says `configured` or it says nothing. The only way to read
these values is to open the files as the service user.

    .venv/bin/python scripts/configure_crm_tms_integration.py --check
    .venv/bin/python scripts/configure_crm_tms_integration.py --apply
    .venv/bin/python scripts/configure_crm_tms_integration.py --verify

`--apply` generates new secrets and rotates both sides together. If
either file cannot be written, both are restored from their backups:
half-rotated credentials are worse than none, because the failure
appears later and somewhere else.
"""
import argparse
import os
import re
import secrets
import shutil
import stat
import sys
from datetime import datetime

CRM_ENV = '/var/www/procam-crm/.env'
TMS_ENV = '/var/www/procam-lr/.env'

#: The name the CRM logs this caller under. Appears in
#: integration_log.caller and in `tms:<name>` on restriction attempts.
CALLER_NAME = 'procam-tms'

CRM_API_BASE = 'https://procamlogitech.com/CRM/api/integration/v1'
TMS_WEBHOOK_URL = 'https://procamlogitech.com/TMS/crm/webhook'

CRM_KEYS = ('CRM_INTEGRATION_TOKENS', 'TMS_WEBHOOK_URL',
            'TMS_WEBHOOK_TOKEN')
TMS_KEYS = ('CRM_BASE_URL', 'CRM_INTEGRATION_TOKEN', 'TMS_WEBHOOK_TOKEN')

#: 48 bytes of randomness, URL-safe base64. The alphabet is
#: [A-Za-z0-9_-], which matters: CRM_INTEGRATION_TOKENS splits on ","
#: and partitions on ":", so a secret containing either would be
#: silently truncated and the comparison would never match.
SECRET_BYTES = 48
_SAFE = re.compile(r'^[A-Za-z0-9_-]+$')


def _new_secret():
    value = secrets.token_urlsafe(SECRET_BYTES)
    if not _SAFE.match(value):
        raise SystemExit('generated a secret with unsafe characters — '
                         'refusing')
    return value


def _read(path):
    out = {}
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, value = line.partition('=')
                out[key.strip()] = value.strip()
    except OSError as exc:
        print(f'could not read {path}: {exc}')
        return None
    return out


def _write(path, updates, heading):
    """Update named keys in place; append any that are missing.

    Line by line, so comments, blank lines, ordering and every
    unrelated value survive untouched — including the TMS's unquoted
    `MAIL_DEFAULT_SENDER=Procam TMS <tms@...>`, which a naive
    rewriter would mangle.
    """
    with open(path, encoding='utf-8') as fh:
        lines = fh.read().splitlines()

    seen, out = set(), []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith('#') and '=' in stripped:
            key = stripped.partition('=')[0].strip()
            if key in updates:
                out.append(f'{key}={updates[key]}')
                seen.add(key)
                continue
        out.append(line)

    missing = [k for k in updates if k not in seen]
    if missing:
        out.append('')
        out.append(f'# ── {heading} ──')
        for key in missing:
            out.append(f'{key}={updates[key]}')

    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')


def _backup(path):
    stamp = datetime.now().strftime('%Y-%m-%d-%H%M%S')
    target = f'{path}.backup-{stamp}'
    shutil.copy2(path, target)
    os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    return target


def _restrict(path):
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return oct(stat.S_IMODE(os.stat(path).st_mode))


def _report(label, path, keys):
    values = _read(path) or {}
    print(f'\n{label}:')
    for key in keys:
        state = 'configured' if (values.get(key) or '').strip() \
                else 'NOT configured'
        print(f'  {key} = {state}')
    return all((values.get(k) or '').strip() for k in keys)


def _pairing_holds(crm_env=CRM_ENV, tms_env=TMS_ENV):
    """Do the two sides actually match? Compared, never shown.

    The whole point of the exercise is that these line up. Checking
    it here means a mismatch is found now rather than as a 401 during
    the TMS deploy.
    """
    import hmac

    crm = _read(crm_env) or {}
    tms = _read(tms_env) or {}

    registry = crm.get('CRM_INTEGRATION_TOKENS') or ''
    crm_token = ''
    for part in registry.split(','):
        name, _, token = part.strip().partition(':')
        if name.strip() == CALLER_NAME:
            crm_token = token.strip()
    bearer_ok = bool(crm_token) and hmac.compare_digest(
        crm_token, (tms.get('CRM_INTEGRATION_TOKEN') or '').strip())
    webhook_ok = bool((crm.get('TMS_WEBHOOK_TOKEN') or '').strip()) and \
        hmac.compare_digest((crm.get('TMS_WEBHOOK_TOKEN') or '').strip(),
                            (tms.get('TMS_WEBHOOK_TOKEN') or '').strip())
    return bearer_ok, webhook_ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--crm-env', default=CRM_ENV)
    ap.add_argument('--tms-env', default=TMS_ENV)
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--verify', action='store_true')
    args = ap.parse_args()

    for path in (args.crm_env, args.tms_env):
        if not os.path.exists(path):
            print(f'not found: {path}')
            return 2

    if args.verify or not (args.apply or args.check):
        crm_ok = _report('CRM', args.crm_env, CRM_KEYS)
        tms_ok = _report('TMS', args.tms_env, TMS_KEYS)
        bearer, webhook = _pairing_holds(args.crm_env, args.tms_env)
        print(f'\n  integration bearer matches across both: {bearer}')
        print(f'  webhook secret matches across both:      {webhook}')
        return 0 if (crm_ok and tms_ok and bearer and webhook) else 1

    if args.check:
        print('WOULD WRITE\n')
        print(f'  {args.crm_env}')
        for key in CRM_KEYS:
            shape = {'CRM_INTEGRATION_TOKENS':
                     f'{CALLER_NAME}:<integration bearer>',
                     'TMS_WEBHOOK_URL': TMS_WEBHOOK_URL,
                     'TMS_WEBHOOK_TOKEN': '<webhook secret>'}[key]
            print(f'    {key}={shape}')
        print(f'\n  {args.tms_env}')
        for key in TMS_KEYS:
            shape = {'CRM_BASE_URL': CRM_API_BASE,
                     'CRM_INTEGRATION_TOKEN': '<integration bearer>',
                     'TMS_WEBHOOK_TOKEN': '<webhook secret>'}[key]
            print(f'    {key}={shape}')
        print('\n  Two independent secrets, 48 random bytes each, '
              'generated at --apply and never printed.')
        print('  Every other variable in both files is left as it is.')
        print('\n== CHECK — nothing written, nothing generated ==')
        return 0

    # ── apply ────────────────────────────────────────────────────────
    bearer, webhook = _new_secret(), _new_secret()
    if bearer == webhook:                       # astronomically unlikely
        raise SystemExit('the two secrets came out identical — refusing')

    crm_backup = _backup(args.crm_env)
    tms_backup = _backup(args.tms_env)
    print(f'  backup  {crm_backup}')
    print(f'  backup  {tms_backup}')

    try:
        _write(args.crm_env, {
            'CRM_INTEGRATION_TOKENS': f'{CALLER_NAME}:{bearer}',
            'TMS_WEBHOOK_URL': TMS_WEBHOOK_URL,
            'TMS_WEBHOOK_TOKEN': webhook,
        }, 'CRM ↔ TMS integration (configure_crm_tms_integration.py)')
        print(f'  CRM     written, mode {_restrict(args.crm_env)}')

        _write(args.tms_env, {
            'CRM_BASE_URL': CRM_API_BASE,
            'CRM_INTEGRATION_TOKEN': bearer,
            'TMS_WEBHOOK_TOKEN': webhook,
        }, 'CRM integration (configured from the CRM side)')
        print(f'  TMS     written, mode {_restrict(args.tms_env)}')
    except Exception as exc:                                  # noqa: BLE001
        # Half-rotated credentials are worse than none: the failure
        # would surface later, somewhere else, as a 401 nobody can
        # explain.
        shutil.copy2(crm_backup, args.crm_env)
        shutil.copy2(tms_backup, args.tms_env)
        _restrict(args.crm_env)
        _restrict(args.tms_env)
        print(f'\n  FAILED: {exc}')
        print('  Both files restored from their backups. Nothing was '
              'rotated.')
        return 1
    finally:
        del bearer, webhook

    crm_ok = _report('CRM', args.crm_env, CRM_KEYS)
    tms_ok = _report('TMS', args.tms_env, TMS_KEYS)
    pair_bearer, pair_webhook = _pairing_holds(args.crm_env,
                                                args.tms_env)
    print(f'\n  integration bearer matches across both: {pair_bearer}')
    print(f'  webhook secret matches across both:      {pair_webhook}')

    if not (crm_ok and tms_ok and pair_bearer and pair_webhook):
        print('\n  Something did not land. Restore from the backups '
              'above before going further.')
        return 1
    print('\nNow: sudo systemctl restart procam-crm')
    print('The TMS needs its own restart to read these — not yet.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
