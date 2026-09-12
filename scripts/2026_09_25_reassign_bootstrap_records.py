"""
Move the records that still name PCM001 off it, so it can be removed.

1,427 rows on production point at the bootstrap account. They are not
all the same kind of thing, and treating them the same would either
block the deletion forever or falsify the record:

  live ownership — companies.pic_emp_code (267) and
      crm_account_members.user_id (260). These say who is responsible
      for an account *today*. Pointing at a deactivated non-person means
      267 companies look owned and are not, which is worse than being
      visibly unowned: they do not even appear in the "Companies with no
      account owner" data-quality check. These must move to a real
      person.

  historical provenance — companies.created_by (893) and
      outreach_drafts.generated_by (6). These record who did something
      in the past. Reassigning them to a real employee would be a lie:
      that person did not create those companies. They can be cleared to
      NULL, which honestly says "unknown", or left as they are.

  the account's own row — access_profiles.emp_code (1). Goes with the
      account whenever it goes.

Usage
    python scripts/2026_09_25_reassign_bootstrap_records.py --check
    python scripts/2026_09_25_reassign_bootstrap_records.py --owner DIR12010
    python scripts/2026_09_25_reassign_bootstrap_records.py --owner DIR12010 \\
        --clear-provenance

Back up first — this writes to live records:
    cp procam_crm.db procam_crm.db.bak-$(date +%F-%H%M)
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

CODE = 'PCM001'

#: (table, column) — who is responsible now. Must point at a real person.
LIVE_OWNERSHIP = (
    ('companies', 'pic_emp_code'),
    ('crm_account_members', 'user_id'),
    ('contacts', 'assigned_to'),
    ('leads', 'assigned_to'),
    ('opportunities', 'owner_emp_code'),
)

#: (table, column) — who did something once. Cleared, never reassigned.
PROVENANCE = (
    ('companies', 'created_by'),
    ('outreach_drafts', 'generated_by'),
    ('import_batches', 'created_by'),
    ('master_items', 'created_by'),
)

#: Belongs to the account itself.
OWN_ROWS = (('access_profiles', 'emp_code'),)


def _count(db, table, col, code):
    try:
        return db.session.execute(db.text(
            f'SELECT COUNT(*) FROM {table} WHERE {col} = :c'),
            {'c': code}).scalar() or 0
    except Exception:
        return None      # table absent in this deployment


def survey(db):
    out = {}
    for group, pairs in (('live', LIVE_OWNERSHIP),
                         ('provenance', PROVENANCE),
                         ('own', OWN_ROWS)):
        rows = {}
        for table, col in pairs:
            n = _count(db, table, col, CODE)
            if n:
                rows[f'{table}.{col}'] = (table, col, n)
        out[group] = rows
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true', help='dry-run')
    ap.add_argument('--owner', help='emp_code to take over live ownership')
    ap.add_argument('--clear-provenance', action='store_true',
                    help='set the historical created_by / generated_by '
                         'fields to NULL (honest "unknown"); without this '
                         'they keep naming PCM001')
    args = ap.parse_args()

    from app import app as flask_app, db, Employee            # noqa: E402

    with flask_app.app_context():
        found = survey(db)

        print(f'  rows still naming {CODE}:\n')
        for group, label in (('live', 'live ownership — must move to a person'),
                             ('provenance', 'historical — cleared, not reassigned'),
                             ('own', "the account's own row")):
            rows = found[group]
            total = sum(v[2] for v in rows.values())
            print(f'  {label}: {total}')
            for where, (_t, _c, n) in sorted(rows.items(),
                                             key=lambda kv: -kv[1][2]):
                print(f'      {n:>6}  {where}')
            print()

        if not args.owner:
            print('  Nothing changed. Re-run with --owner <EMP_CODE> to move '
                  'live ownership.')
            print('  Add --clear-provenance to also blank the historical '
                  'fields.')
            return

        target = Employee.query.filter_by(emp_code=args.owner).first()
        if target is None:
            raise SystemExit(f'  {args.owner} is not an employee.')
        if not target.is_active:
            raise SystemExit(f'  {args.owner} ({target.name}) is not active — '
                             f'that would just move the problem.')
        print(f'  live ownership → {target.emp_code} {target.name}')

        moved = cleared = 0
        for _where, (table, col, n) in found['live'].items():
            if args.check:
                moved += n
                continue
            moved += db.session.execute(db.text(
                f'UPDATE {table} SET {col} = :new WHERE {col} = :old'),
                {'new': args.owner, 'old': CODE}).rowcount

        if args.clear_provenance:
            for _where, (table, col, n) in found['provenance'].items():
                if args.check:
                    cleared += n
                    continue
                cleared += db.session.execute(db.text(
                    f'UPDATE {table} SET {col} = NULL WHERE {col} = :old'),
                    {'old': CODE}).rowcount

        if args.check:
            print(f'\n== DRY-RUN — nothing written ==')
            print(f'  WOULD reassign {moved} live-ownership row(s)')
            print(f'  WOULD clear {cleared} provenance row(s)'
                  if args.clear_provenance else
                  '  provenance rows left naming PCM001 '
                  '(pass --clear-provenance to blank them)')
            return

        db.session.commit()
        print(f'\n  reassigned {moved} live-ownership row(s)')
        if args.clear_provenance:
            print(f'  cleared {cleared} provenance row(s) to NULL')
        left = survey(db)
        remaining = sum(v[2] for g in left.values() for v in g.values())
        print(f'  rows still naming {CODE}: {remaining}')
        if remaining:
            print('  (the account cannot be deleted until these are zero — '
                  'run with\n   --clear-provenance, or leave it deactivated)')


if __name__ == '__main__':
    main()
