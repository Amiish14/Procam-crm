"""
Remove the PCM001 bootstrap account.

PCM001 is seeded from ADMIN_INITIAL_PASSWORD so a fresh install can be
opened at all.  It is not a person: it has role='admin', which in this
codebase means every permission, and its password lives in .env.  Once
the real administrators exist it is a standing credential nobody owns.

Two modes:

    (default)  deactivate — it can no longer sign in, but the row stays,
               which keeps any created_by / performed_by reference to it
               meaningful and leaves --unlock as a way back in.

    --delete   remove the row entirely. Every column in the database
               that can hold an employee code is scanned first; if
               anything still points at PCM001 the deletion is refused
               and the references are listed, because deleting it then
               would leave records attributed to an account that no
               longer exists.

Getting back in, if you ever need to
    python scripts/2026_09_24_lock_bootstrap_account.py --unlock
    (then log in, do what you need, and run this again)

Usage
    python scripts/2026_09_24_lock_bootstrap_account.py --check
    python scripts/2026_09_24_lock_bootstrap_account.py
    python scripts/2026_09_24_lock_bootstrap_account.py --unlock

Refuses to lock the account when no other active admin exists, because
that would leave the portal with no way in at all.
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


#: Columns whose name matches the emp-code heuristic but which hold
#: something else entirely.
_NOT_EMP_CODES = {
    ('lead_attachments', 'size_bytes'),
    ('rfqs', 'quote_by_date'),
    ('rate_sourcing_lines', 'required_by'),
    ('task_definitions', 'next_owner_rule'),
    ('leads', 'secondary_owner_name'),
    ('lead_notes', 'author_name'),
    ('task_instances', 'owner_role'),
}


def find_references(db, code):
    """Every row anywhere that still names this employee.

    Derived from the live schema rather than a hand-kept list, so a table
    added later is scanned too.
    """
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    found = {}
    for table in insp.get_table_names():
        if table == 'employees':
            continue
        for col in insp.get_columns(table):
            name = col['name']
            if (table, name) in _NOT_EMP_CODES:
                continue
            if not any(k in name for k in ('emp_code', '_by', 'author',
                                           'owner', 'assigned_to',
                                           'user_id', 'pic_emp')):
                continue
            if 'CHAR' not in str(col['type']).upper() \
                    and 'TEXT' not in str(col['type']).upper():
                continue
            try:
                n = db.session.execute(db.text(
                    f'SELECT COUNT(*) FROM {table} WHERE {name} = :c'),
                    {'c': code}).scalar() or 0
            except Exception:
                continue
            if n:
                found[f'{table}.{name}'] = n
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true', help='dry-run')
    ap.add_argument('--unlock', action='store_true',
                    help='re-activate it (emergency access)')
    ap.add_argument('--delete', action='store_true',
                    help='remove the row entirely, if nothing references it')
    ap.add_argument('--force', action='store_true',
                    help='delete even though references exist (they will be '
                         'left pointing at a missing account)')
    args = ap.parse_args()

    from app import app as flask_app, db, Employee            # noqa: E402

    with flask_app.app_context():
        pcm = Employee.query.filter_by(emp_code=CODE).first()
        if pcm is None:
            print(f'  {CODE} does not exist — nothing to do.')
            return

        others = (Employee.query
                  .filter(Employee.emp_code != CODE,
                          Employee.is_active.is_(True),
                          Employee.role.in_(('admin', 'procam_admin')))
                  .all())
        print(f'  {CODE}: active={pcm.is_active} role={pcm.role} '
              f'never_logged_in={bool(pcm.must_change_pw)}')
        print(f'  other active admins: {len(others)}')
        for e in others[:10]:
            print(f'    {e.emp_code}  {e.name}')

        if args.unlock:
            if args.check:
                print(f'\n== DRY-RUN ==\n  WOULD re-activate {CODE}.')
                return
            pcm.is_active = True
            pcm.must_change_pw = True
            db.session.commit()
            print(f'\n  {CODE} re-activated, and must change its password on '
                  f'login.\n  Lock it again as soon as you are done.')
            return

        if not others:
            raise SystemExit(
                f'\n  Refusing to lock {CODE}: it is the only active admin, '
                f'so locking it\n  would leave the portal with no way in.')

        if args.delete:
            refs = find_references(db, CODE)
            if refs:
                print(f'\n  {sum(refs.values())} row(s) still name {CODE}:')
                for where, count in sorted(refs.items(),
                                           key=lambda kv: -kv[1]):
                    print(f'    {count:>6}  {where}')
                if not args.force:
                    raise SystemExit(
                        f'\n  Refusing to delete {CODE}: those records would '
                        f'be left attributed to\n  an account that does not '
                        f'exist. Either reassign them first, or keep the\n'
                        f'  account deactivated (the default mode), which '
                        f'blocks sign-in and keeps\n  the history readable. '
                        f'--force overrides this.')
                print('\n  --force given: deleting anyway.')
            else:
                print(f'\n  Nothing references {CODE}.')

            if args.check:
                print(f'\n== DRY-RUN — nothing written ==')
                print(f'  WOULD delete the {CODE} row.')
                return
            db.session.delete(pcm)
            db.session.commit()
            print(f'\n  {CODE} deleted.')
            print(f'  It is seeded on first boot from ADMIN_INITIAL_PASSWORD, '
                  f'so it will come\n  back if the app ever starts against a '
                  f'database with no PCM001 row.\n  Remove ADMIN_INITIAL_'
                  f'PASSWORD from .env to stop that.')
            return

        if pcm.is_active is False:
            print(f'\n  {CODE} is already inactive — nothing to do.')
            return

        if args.check:
            print(f'\n== DRY-RUN — nothing written ==')
            print(f'  WOULD deactivate {CODE}.')
            print(f'  {len(others)} other admin(s) keep their access.')
            print(f'  Reversible with --unlock.')
            return

        pcm.is_active = False
        db.session.commit()
        print(f'\n  {CODE} deactivated. It can no longer sign in.')
        print(f'  The row is kept: created_by fields reference it, and '
              f'--unlock is the\n  documented way back in if every admin is '
              f'ever locked out.')


if __name__ == '__main__':
    main()
