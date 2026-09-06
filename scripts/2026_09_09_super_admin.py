#!/usr/bin/env python3
"""
Super admin — one account owns the Access Control matrix and always sees
the whole company.

    python scripts/2026_09_09_super_admin.py --check   # dry run
    python scripts/2026_09_09_super_admin.py           # apply

Adds employees.is_super_admin and sets it for DIR12010 (Nilesh Kumar
Sinha).  Idempotent.

Why a column and not a checkbox: the super admin owns the screen that
grants permissions.  If it were grantable there, someone could promote
themselves, or the owner could be edited out of their own matrix with no
way back except the shell.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)


def _resolve_db_url():
    """The database URI, computed the same way app.py does.

    This runs BEFORE importing app.py, because app.py calls init_db() at
    import time and that query fails while the new column is missing.

    app.py loads .env itself, so this has to as well — without it the
    script reads an unset DATABASE_URL, falls back to the default, and
    quietly migrates the wrong file.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_ROOT, '.env'))
    except Exception:
        pass
    url = os.environ.get('DATABASE_URL', 'sqlite:///procam_crm.db')
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql://', 1)
    if url.startswith('sqlite:///') and not url.startswith('sqlite:////'):
        # Flask-SQLAlchemy resolves a relative sqlite path against the
        # Flask instance folder.
        rel = url[len('sqlite:///'):]
        if not os.path.isabs(rel):
            cand = os.path.join(_ROOT, 'instance', rel)
            if not os.path.exists(cand):
                alt = os.path.join(_ROOT, rel)
                cand = alt if os.path.exists(alt) else cand
            url = 'sqlite:///' + cand
    return url


def add_column_if_missing(dry):
    """Add employees.is_super_admin using a bare engine, no app import."""
    from sqlalchemy import create_engine, inspect, text

    url = _resolve_db_url()
    if url.startswith('sqlite:///'):
        path = url[len('sqlite:///'):]
        print(f'database: {path}')
        if not os.path.exists(path):
            print('!! that file does not exist — check DATABASE_URL')
            return False
    else:
        print(f'database: {url.split("@")[-1]}')

    engine = create_engine(url)

    # Prove this is the live database before altering it.  An empty
    # employees table means the wrong file was resolved.
    with engine.connect() as conn:
        n_emp = conn.execute(text('SELECT COUNT(*) FROM employees')).scalar()
    print(f'employees in this database: {n_emp}')
    if not n_emp:
        print('!! no employees here — this is not the live database.')
        print('   Check DATABASE_URL in .env, then re-run.')
        engine.dispose()
        return False

    cols = {c['name'] for c in inspect(engine).get_columns('employees')}
    if 'is_super_admin' in cols:
        print('employees.is_super_admin — already present')
        engine.dispose()
        return True

    print('ADD COLUMN employees.is_super_admin' + ('   (dry run)' if dry else ''))
    if not dry:
        with engine.begin() as conn:
            conn.execute(text('ALTER TABLE employees '
                              'ADD COLUMN is_super_admin BOOLEAN DEFAULT 0'))
        print('  ✓ added')
    engine.dispose()
    return not dry


SUPER_ADMIN = 'DIR12010'          # Nilesh Kumar Sinha


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--emp-code', default=SUPER_ADMIN,
                    help=f'who becomes super admin (default {SUPER_ADMIN})')
    args = ap.parse_args()
    dry = args.check

    ready = add_column_if_missing(dry)
    if not ready:
        print('\n(dry run: re-run without --check to add the column, '
              'then the grant can be previewed)'
              if dry else '\nAborted.')
        return 0 if dry else 1

    # Safe to import now that the column exists.
    import importlib
    _main = importlib.import_module('app')
    importlib.import_module('app.models')
    app, db, Employee = _main.app, _main.db, _main.Employee

    with app.app_context():
        target = Employee.query.filter_by(emp_code=args.emp_code).first()
        if target is None:
            print(f'!! {args.emp_code} not found — nothing granted')
            return 1

        current = Employee.query.filter_by(is_super_admin=True).all()
        for e in current:
            if e.emp_code != args.emp_code:
                print(f'  → {e.emp_code} {e.name}: no longer super admin')
                if not dry:
                    e.is_super_admin = False

        if target.is_super_admin:
            print(f'  = {target.emp_code} {target.name} — already super admin')
        else:
            print(f'  → {target.emp_code} {target.name}: SUPER ADMIN')
            if not dry:
                target.is_super_admin = True

        if not dry:
            db.session.commit()

        print('\nWho sees what now')
        from app.access.service import effective
        for e in Employee.query.filter_by(is_active=True)\
                               .order_by(Employee.name).all():
            scope, perms = effective(e.emp_code)
            if scope == 'all' or e.is_vertical_head or e.role == 'admin' \
               or e.is_super_admin:
                tag = ('SUPER ADMIN' if e.is_super_admin
                       else 'admin' if e.role == 'admin'
                       else 'vertical head' if e.is_vertical_head else '')
                print(f'  {e.emp_code:12} {e.name:26} {scope:9}'
                      f' {len(perms):2} perms   {tag}')

        print('\n' + ('Dry run — nothing written.' if dry else '✓ applied'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
