"""
Generic employee upsert CLI for the Procam CRM.

Adds an employee to the Employee master (or updates an existing one) with
a specified role. Idempotent — safe to re-run. First-login password is
the employee code (CRM forces a password change on first sign-in).

Usage:
    python scripts/add_employee.py \\
        --emp-code CON302026 \\
        --name "TAMAL DAS" \\
        --email tamal.das@procamgroup.in \\
        --role user \\
        --department "Consulting" \\
        --designation "Consultant" \\
        --vertical "Heavy Transport"

Roles the CRM recognises: admin | sales | presales | user
"""
import argparse
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import app, db, Employee


VALID_ROLES = {'admin', 'sales', 'presales', 'user'}


def main():
    ap = argparse.ArgumentParser(description='Add or update a CRM employee.')
    ap.add_argument('--emp-code',    required=True, help='e.g. CON302026 — used as username.')
    ap.add_argument('--name',        required=True)
    ap.add_argument('--email',       required=True)
    ap.add_argument('--mobile',      default=None)
    ap.add_argument('--role',        required=True, choices=sorted(VALID_ROLES))
    ap.add_argument('--department',  default=None)
    ap.add_argument('--designation', default=None)
    ap.add_argument('--vertical',    default=None)
    args = ap.parse_args()

    with app.app_context():
        emp = Employee.query.filter_by(emp_code=args.emp_code).first()
        if emp is None:
            emp = Employee(emp_code=args.emp_code, name=args.name)
            db.session.add(emp)
            action = 'created'
        else:
            action = 'updated'

        emp.name         = args.name
        emp.email        = args.email
        emp.mobile       = args.mobile or emp.mobile
        emp.department   = args.department or emp.department
        emp.designation  = args.designation or emp.designation
        emp.vertical     = args.vertical or emp.vertical
        emp.role         = args.role
        emp.is_active    = True
        emp.must_change_pw = True
        # First-login password mirrors the employee code — same convention
        # every other Procam CRM login uses. User is forced to change it.
        emp.set_password(args.emp_code)

        db.session.commit()
        print(f'  [OK] {action}: {emp.emp_code}  {emp.name}  role={emp.role}')
        print(f'       First-login password = {args.emp_code}  (force-change on sign-in)')


if __name__ == '__main__':
    main()
