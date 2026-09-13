"""
Which accounts still open with their employee code? Read-only.

Every seeded account, and every account created or reset before the
2026-09 hardening, was given its employee code in lowercase as its
password. must_change_pw forces a change at the next login, but an
account nobody has logged into still opens with a code anyone in the
company can guess.

    python scripts/audit_default_passwords.py
    python scripts/audit_default_passwords.py --list

Prints counts, and with --list the employee codes. Never prints or
stores a password or hash. Opens the database read-only and does not
import the app. Remediation is a person's decision, not this script's:
reset each listed account from Employees (it now issues a random
temporary password) or deactivate the ones nobody uses.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from data_quality_report import read_only_engine              # noqa: E402
from sqlalchemy import text                                   # noqa: E402
from werkzeug.security import check_password_hash             # noqa: E402


def exposed(conn):
    """Active accounts whose password is their employee code."""
    rows = conn.execute(text(
        "SELECT emp_code, role, COALESCE(is_super_admin, 0) AS is_super, "
        "       password_hash FROM employees WHERE is_active = 1")).fetchall()
    out = []
    for code, role, is_super, pw_hash in rows:
        if not pw_hash or not code:
            continue
        if any(check_password_hash(pw_hash, guess)
               for guess in (code.lower(), code)):
            out.append({'emp_code': code, 'role': role or '',
                        'is_super_admin': bool(is_super)})
    return out, len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--list', action='store_true',
                    help='print the affected employee codes')
    args = ap.parse_args()
    with read_only_engine().connect() as conn:
        hits, total = exposed(conn)
    admins = [h for h in hits if h['role'] in ('admin', 'procam_admin')]
    print(f'\n  active accounts checked            {total}')
    print(f'  password is the employee code      {len(hits)}')
    print(f'    of which admins                  {len(admins)}')
    print(f'    of which the super admin         '
          f'{sum(h["is_super_admin"] for h in hits)}')
    if args.list:
        for h in sorted(hits, key=lambda h: (h['role'], h['emp_code'])):
            print(f'    {h["emp_code"]:<12} {h["role"]}')
    return 1 if hits else 0


if __name__ == '__main__':
    raise SystemExit(main())
