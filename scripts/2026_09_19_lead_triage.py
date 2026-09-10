"""
Lead Triage permission — WP4.

The dashboard is gated by `admin.triage`, a normal entry in the access
matrix.  Admins and the super admin get every permission by default, so
they need no migration — but anyone with a *stored* access profile has a
fixed permission list that predates this one, and would silently not see
the dashboard even if they are an administrator.

This grants admin.triage to stored profiles that already hold
admin.access, and to nobody else.

Usage:
    python scripts/2026_09_19_lead_triage.py --check    # dry-run
    python scripts/2026_09_19_lead_triage.py            # apply
    python scripts/2026_09_19_lead_triage.py --down     # revoke it again
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, '.env'))
except ImportError:
    pass

PERM = 'admin.triage'

#: Bootstrap accounts, which are not people. PCM001 is seeded from
#: ADMIN_INITIAL_PASSWORD so the portal can be opened on a fresh install;
#: it belongs to nobody, and widening what it can reach widens what a
#: leaked bootstrap password reaches. New permissions are granted to
#: named employees, never to it.
BOOTSTRAP_ACCOUNTS = {'PCM001'}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true', help='dry-run')
    ap.add_argument('--down', action='store_true', help='revoke the grant')
    args = ap.parse_args()

    from app import app as flask_app, db, Employee          # noqa: E402
    from app.models.access import AccessProfile             # noqa: E402

    with flask_app.app_context():
        if not Employee.query.count():
            raise SystemExit(
                'Refusing to run: no employees in this database.')

        changed = []
        skipped = []
        for prof in AccessProfile.query.all():
            if prof.emp_code in BOOTSTRAP_ACCOUNTS:
                skipped.append(prof.emp_code)
                continue
            perms = list(prof.perm_set())
            if args.down:
                if PERM in perms:
                    perms.remove(PERM)
                    changed.append((prof.emp_code, 'revoked'))
                else:
                    continue
            else:
                if 'admin.access' not in perms or PERM in perms:
                    continue
                perms.append(PERM)
                changed.append((prof.emp_code, 'granted'))
            if not args.check:
                prof.perms = sorted(perms)

        if args.check:
            print('== DRY-RUN — nothing written ==')
        for code, what in changed:
            print(f'  {what} {PERM} to {code}')
        for code in skipped:
            print(f'  skipped {code} — bootstrap account, not a person')
        if not changed:
            print(f'  no stored profile needed a change — admins and the '
                  f'super admin already hold {PERM} by default.')
        if not args.check:
            db.session.commit()
            print('  done.')


if __name__ == '__main__':
    main()
