"""
Give the CRM the SMTP settings the TMS already uses.

The credentials exist and work; the CRM simply does not have them.
This copies the handful of values it needs from the TMS's environment
file into the CRM's own, and nothing else.

What it will not do, because this went wrong once already:

  * it does not source the TMS file — a `.env` sourced into a shell is
    executed, and the TMS's unquoted `MAIL_DEFAULT_SENDER=Procam TMS
    <tms@…>` is enough to break bash after it has already exported
    `DATABASE_URL` at every later command
  * it copies only the MAIL_* keys named below. `DATABASE_URL` and
    everything else in the TMS file stays where it is
  * it never prints a value. Secrets appear in the output as
    "set, N characters" and nowhere else
  * it backs the CRM file up first, timestamped, and keeps 0600

    .venv/bin/python scripts/configure_crm_smtp.py --check
    .venv/bin/python scripts/configure_crm_smtp.py --apply

`--check` writes nothing and shows exactly which keys would change.
"""
import argparse
import os
import shutil
import stat
import sys
from datetime import datetime

#: Copied verbatim from the TMS file.
COPY_FROM_TMS = ('MAIL_SERVER', 'MAIL_PORT', 'MAIL_USE_TLS',
                 'MAIL_USE_SSL', 'MAIL_USERNAME', 'MAIL_PASSWORD')

#: The CRM decides these for itself. The sender address is the TMS's
#: mailbox because that is the account that may send; the display name
#: is the CRM's, so the person reading it knows which system wrote.
CRM_OWN = {
    'CRM_MAIL_TRANSPORT': 'smtp',
    'CRM_MAIL_FROM': 'Procam CRM <tms@procamgroup.in>',
}

#: Never copied, whatever the source file holds.
NEVER = ('DATABASE_URL', 'SECRET_KEY', 'MAIL_DEFAULT_SENDER')

SECRET_KEYS = ('MAIL_PASSWORD',)


def _mask(key, value):
    if key in SECRET_KEYS:
        return f'set, {len(value)} characters' if value else '(empty)'
    return value


def _read_env(path):
    """KEY → value. Parsed, never executed."""
    out = {}
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, value = line.partition('=')
                key, value = key.strip(), value.strip()
                if len(value) >= 2 and value[0] == value[-1] in ('"', "'"):
                    value = value[1:-1]
                out[key] = value
    except OSError as exc:
        print(f'could not read {path}: {exc}')
        return None
    return out


def _write_env(path, updates):
    """Update keys in place; append the ones that are missing.

    Rewrites the file line by line so comments, blank lines and the
    order of everything else survive — a CRM `.env` that came back
    reordered and stripped of its comments would be a worse outcome
    than not having mail.
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
        out.append('# ── Outbound mail (added by '
                   'scripts/configure_crm_smtp.py) ──')
        out.append('# The same SMTP account the TMS sends with. Setting '
                   'MAIL_SERVER is')
        out.append('# what switches the CRM off Microsoft Graph, which '
                   'has never worked')
        out.append('# because the Mail.Send grant does not exist. '
                   'Removing these lines')
        out.append('# is the whole rollback.')
        for key in missing:
            out.append(f'{key}={updates[key]}')

    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')
    return seen, missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tms-env', default='/var/www/procam-lr/.env')
    ap.add_argument('--crm-env', default='/var/www/procam-crm/.env')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args()

    if not args.apply:
        args.check = True

    source = _read_env(args.tms_env)
    if source is None:
        return 2
    if not os.path.exists(args.crm_env):
        print(f'the CRM environment file is not at {args.crm_env}')
        return 2

    target = _read_env(args.crm_env) or {}

    updates, missing_from_source = {}, []
    for key in COPY_FROM_TMS:
        if key in source and source[key] != '':
            updates[key] = source[key]
        else:
            missing_from_source.append(key)
    updates.update(CRM_OWN)

    print(f'source : {args.tms_env}')
    print(f'target : {args.crm_env}\n')

    if missing_from_source:
        print('not present in the TMS file (left unset in the CRM):')
        for key in missing_from_source:
            print(f'  {key}')
        print()

    print('WHAT WOULD CHANGE' if args.check else 'CHANGED')
    for key, value in updates.items():
        before = target.get(key)
        if before is None:
            state = 'add'
        elif before == value:
            state = 'unchanged'
        else:
            state = 'replace'
        print(f'  {state:<10} {key:<22} {_mask(key, value)}')

    leaked = [k for k in NEVER if k in updates]
    if leaked:
        print(f'\nREFUSING: would have copied {leaked}. That is a bug — '
              f'nothing in NEVER may be written.')
        return 2

    untouched = [k for k in target if k not in updates]
    print(f'\n  {len(untouched)} other CRM variable(s) left exactly as '
          f'they are.')

    if args.check:
        print('\n== CHECK — nothing written ==')
        print('Re-run with --apply to write them.')
        return 0

    stamp = datetime.now().strftime('%Y-%m-%d-%H%M%S')
    backup = f'{args.crm_env}.backup-{stamp}'
    shutil.copy2(args.crm_env, backup)
    os.chmod(backup, stat.S_IRUSR | stat.S_IWUSR)
    print(f'\n  backup   {backup} (0600)')

    replaced, added = _write_env(args.crm_env, updates)
    os.chmod(args.crm_env, stat.S_IRUSR | stat.S_IWUSR)
    mode = stat.S_IMODE(os.stat(args.crm_env).st_mode)
    print(f'  updated  {len(replaced)} key(s) in place, {len(added)} '
          f'appended')
    print(f'  mode     {oct(mode)}')

    after = _read_env(args.crm_env) or {}
    wrong = [k for k, v in updates.items() if after.get(k) != v]
    if wrong:
        print(f'\n  VERIFY FAILED for {wrong} — restore {backup}')
        return 1
    print('  verified — every key reads back as intended')
    print('\nNow: sudo systemctl restart procam-crm')
    return 0


if __name__ == '__main__':
    sys.exit(main())
