"""Why can this person not sign in. READ ONLY, ALWAYS.

CRM-WA-01 §5.

    .venv/bin/python scripts/signin_check.py --code EMP372011
    .venv/bin/python scripts/signin_check.py --code EMP372011 --failures

`/login` answers "Invalid Employee Code or Password" for every
failure, on purpose — telling an anonymous caller which half is wrong
tells them which codes exist. This is the answer for an administrator
standing at the server, and it names which of the five conditions the
sign-in actually tripped.

The one that catches people: **a deactivated account is refused
before the password is checked**, so it gives exactly the same message
as a wrong password — and resetting the password will not fix it.

IT WRITES NOTHING

No `--apply`, no reset, no flag. The reset that fixes most of these
already exists at `PUT /api/employees/<id>` with
`{"reset_password": true}`, behind the Reset password button on
Admin → Employees, and it clears a lock and signs out the old
sessions at the same time. A second way to issue a credential is a
second thing to get wrong.

It never reads, prints or logs a password or a hash, and it cannot
tell you whether a particular password would work.
"""
from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import app, db                                   # noqa: E402
from app.services import signin_diagnosis as sd           # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--code', required=True, action='append',
                    metavar='EMP_CODE',
                    help='the employee code to look up. Repeatable.')
    ap.add_argument('--failures', action='store_true',
                    help='also list the recent failed attempts')
    args = ap.parse_args(argv)

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('CRM-WA-01 §5 — sign-in diagnosis. READ ONLY.\n')

        for code in args.code:
            out = sd.diagnose(code)
            print(f'── {out["code"]}'
                  + (f'  ({out["name"]})' if out['name'] else ''))
            if not out['exists']:
                print(f'   VERDICT : {out["verdict"]}')
                print(f'   {out["detail"]}')
                print(f'   DO      : {out["advice"]}\n')
                continue
            print(f'   active  : {"yes" if out["is_active"] else "NO"}')
            print(f'   role    : {out["role"] or "—"}'
                  f'   vertical: {out["vertical"] or "—"}')
            print(f'   email   : {out["email"] or "— none on file"}')
            print(f'   locked  : {out["locked_until"] or "no"}'
                  f'   failed attempts: {out["failed_logins"]}')
            print(f'   temp pw : '
                  + ('must change; expires '
                     + (out['temp_expires_at'] or 'never')
                     if out['must_change_pw'] else 'no'))
            print(f'   last in : {out["last_login"] or "never signed in"}')
            print(f'   VERDICT : {out["verdict"]}')
            print(f'   {out["detail"]}')
            print(f'   DO      : {out["advice"]}')

            if args.failures:
                rows = sd.recent_failures(out['code'])
                if not rows:
                    print('   no failed attempts on record for this code')
                for row in rows:
                    known = ('the code was recognised'
                             if row['known_active_account']
                             else 'the code was NOT recognised as an '
                                  'active account')
                    print(f'     {row["at"]}  {known}'
                          + (f'  from {row["ip"]}' if row['ip'] else ''))
            print()

        # Belt and braces: the reads above should not have dirtied
        # anything, and if something did, it does not survive.
        db.session.rollback()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
