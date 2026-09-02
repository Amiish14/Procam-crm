#!/usr/bin/env python
"""
v2026-09-02 — Set Employee.email from the official directory.

data/employee_directory.csv is the authoritative emp_code -> email mapping
supplied by the business. Matching is EXACT on emp_code — no fuzzy name
guessing — so an address can never land on the wrong person.

Reports four outcomes per row:

    SAME     already correct
    NEW      no email on file, one supplied
    CHANGE   on file differs from the directory
    NO SUCH  emp_code in the CSV but not in the CRM

Also lists CRM employees the directory does not cover, so you can see who
would silently miss notifications.

Examples:
    python scripts/2026_09_02_apply_employee_directory.py            # preview
    python scripts/2026_09_02_apply_employee_directory.py --apply
    python scripts/2026_09_02_apply_employee_directory.py --apply --active-only
"""
import argparse
import csv
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
except ImportError:                                              # pragma: no cover
    pass

from app import app, db, Employee                             # noqa: E402

DEFAULT_CSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'employee_directory.csv')


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true', help='write the changes')
    ap.add_argument('--active-only', action='store_true',
                    help='skip deactivated employees')
    ap.add_argument('--file', default=DEFAULT_CSV)
    args = ap.parse_args()

    with open(args.file) as fh:
        directory = [r for r in csv.DictReader(fh) if (r.get('emp_code') or '').strip()]
    print(f'directory rows : {len(directory)}')

    with app.app_context():
        q = Employee.query
        if args.active_only:
            q = q.filter_by(is_active=True)
        emps = {e.emp_code.upper(): e for e in q.all()}
        print(f'CRM employees  : {len(emps)}'
              f'   (active {Employee.query.filter_by(is_active=True).count()})')
        if not args.apply:
            print('\nPREVIEW — nothing will be written.\n')

        out = Counter()
        covered = set()
        changes = []

        for row in directory:
            code = row['emp_code'].strip().upper()
            addr = (row.get('email') or '').strip()
            if not addr:
                continue
            emp = emps.get(code)
            if emp is None:
                out['NO SUCH emp_code in CRM'] += 1
                continue
            covered.add(code)
            cur = (emp.email or '').strip()
            if cur.lower() == addr.lower():
                out['SAME'] += 1
                continue
            verdict = 'NEW' if not cur else 'CHANGE'
            out[verdict] += 1
            changes.append((emp, cur, addr, verdict))
            if args.apply:
                emp.email = addr

        if args.apply:
            db.session.commit()

        if changes:
            print('%-12s %-30s %-32s %-34s %s'
                  % ('emp_code', 'name', 'current', 'directory', ''))
            print('-' * 118)
            for emp, cur, addr, verdict in changes:
                print('%-12s %-30s %-32s %-34s %s' % (
                    emp.emp_code, (emp.name or '')[:30], (cur or '—')[:32],
                    addr[:34], verdict))

        uncovered = [e for c, e in sorted(emps.items()) if c not in covered]
        if uncovered:
            print(f'\n--- {len(uncovered)} CRM employees not in the directory ---')
            print('(they keep whatever email they have; no address = no alerts)')
            for e in uncovered:
                print('  %-12s %-30s %-34s active=%s' % (
                    e.emp_code, (e.name or '')[:30], (e.email or '—')[:34],
                    e.is_active))

        print('\n--- summary ---')
        for k, n in out.most_common():
            print(f'  {n:>5}  {k}')
        with app.app_context():
            live = Employee.query.filter_by(is_active=True).all()
            print(f'  {sum(1 for e in live if e.email):>5}  active employees '
                  f'with an email (of {len(live)})')
        if not args.apply:
            print('\nPreview only. Add --apply to write.')


if __name__ == '__main__':
    main()
