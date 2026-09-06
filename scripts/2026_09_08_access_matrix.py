#!/usr/bin/env python3
"""
Access Control matrix — create the table, seed profiles, and put the three
vertical directors on their own verticals instead of full admin.

    python scripts/2026_09_08_access_matrix.py --check    # dry run
    python scripts/2026_09_08_access_matrix.py            # apply

Idempotent: re-running changes nothing.

Two things happen:

1.  access_profiles is created and seeded from each employee's current
    role, so nobody's access changes on deploy.

2.  Admin is narrowed to the two people who should hold it:

        DIR12010  Nilesh Kumar Sinha    stays admin
        DIR42010  T G Ramalingam        stays admin

        DIR22010  James Francis Xavier  → head of their vertical
        DIR52011  Sethupathy Sundaram   → head of their vertical
        DIR72012  Srinivas Marella      → head of their vertical

    The three keep every report, but see only their own vertical's rows.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import importlib                                             # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
app, db, Employee = _main.app, _main.db, _main.Employee

from app.models.access import AccessProfile, DataScope        # noqa: E402
from app.access.service import default_for, ALL_PERMS, REPORT_PERMS  # noqa: E402

# Directors who should run their vertical rather than the whole company.
TO_VERTICAL_HEAD = ['DIR22010', 'DIR52011', 'DIR72012']
# Everyone who keeps company-wide admin.
KEEP_ADMIN = ['DIR12010', 'DIR42010']

_HEAD_PERMS = REPORT_PERMS + ['module.rfq', 'module.quotes',
                              'module.handovers', 'module.funnels',
                              'module.competitors', 'module.business_cards']


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true',
                    help='report what would change, write nothing')
    args = ap.parse_args()
    dry = args.check

    with app.app_context():
        # ── 1. table ──────────────────────────────────────────────
        insp = db.inspect(db.engine)
        if 'access_profiles' not in insp.get_table_names():
            print('CREATE TABLE access_profiles'
                  + ('   (dry run)' if dry else ''))
            if not dry:
                AccessProfile.__table__.create(db.engine)
        else:
            print('access_profiles — already present')

        if dry and 'access_profiles' not in db.inspect(db.engine).get_table_names():
            print('\n(dry run: cannot preview seeding until the table exists)')
            return 0

        # ── 2. role changes ───────────────────────────────────────
        print('\nRole changes')
        for code in TO_VERTICAL_HEAD:
            emp = Employee.query.filter_by(emp_code=code).first()
            if emp is None:
                print(f'  !! {code} not found — skipped')
                continue
            vert = (emp.vertical or '').strip()
            if emp.role != 'admin' and emp.is_vertical_head:
                print(f'  = {code} {emp.name} — already vertical head'
                      f' of {vert or "(no vertical)"}')
                continue
            warn = '  ⚠ NO VERTICAL SET' if not vert else ''
            print(f'  → {code} {emp.name}: admin → vertical head'
                  f' of {vert or "(none)"}{warn}')
            if not dry:
                emp.role = 'user'
                emp.is_vertical_head = True

        for code in KEEP_ADMIN:
            emp = Employee.query.filter_by(emp_code=code).first()
            if emp is None:
                print(f'  !! {code} not found')
            else:
                print(f'  = {code} {emp.name} — stays admin')

        if not dry:
            db.session.commit()

        # ── 3. seed profiles ──────────────────────────────────────
        existing = {p.emp_code for p in AccessProfile.query.all()}
        created = 0
        for emp in Employee.query.all():
            if emp.emp_code in existing:
                continue
            if emp.emp_code in TO_VERTICAL_HEAD:
                scope, perms = DataScope.VERTICAL, list(_HEAD_PERMS)
            elif emp.emp_code in KEEP_ADMIN:
                scope, perms = DataScope.ALL, list(ALL_PERMS)
            else:
                scope, perms = default_for(emp)
            if not dry:
                db.session.add(AccessProfile(emp_code=emp.emp_code,
                                             data_scope=scope,
                                             perms=sorted(perms),
                                             updated_by='migration'))
            created += 1
        if not dry:
            db.session.commit()
        print(f'\nProfiles seeded: {created}'
              f' (already had {len(existing)})'
              + ('   (dry run)' if dry else ''))

        # ── 4. summary ────────────────────────────────────────────
        print('\nResulting access')
        for emp in Employee.query.filter_by(is_active=True)\
                                 .order_by(Employee.name).all():
            prof = AccessProfile.query.filter_by(emp_code=emp.emp_code).first()
            if emp.emp_code in TO_VERTICAL_HEAD:
                # In --check nothing is written yet, so show what the run
                # would produce rather than the untouched current state.
                scope, perms = DataScope.VERTICAL, list(_HEAD_PERMS)
            elif prof is None:
                scope, perms = default_for(emp)
            else:
                scope, perms = prof.data_scope, prof.perms or []
            reports = len([p for p in perms if p.startswith('reports.')])
            if scope == DataScope.ALL or reports:
                print(f'  {emp.emp_code:12} {emp.name:28}'
                      f' {DataScope.LABELS.get(scope, scope):20}'
                      f' {reports}/3 report groups')

        print('\n' + ('Dry run — nothing written. Re-run without --check.'
                      if dry else '✓ applied'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
