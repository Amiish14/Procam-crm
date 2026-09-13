"""Quick diagnostic — reset PCM001 password and dump lead/opp counts.

    python scripts/diagnose.py                          # counts only
    python scripts/diagnose.py --password-file PATH     # also reset PCM001

The password is read from the first line of PATH. It is never kept in
source: a password written here is published to everyone with the repo.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import app, db, Employee, Lead, Opportunity, Company
from werkzeug.security import generate_password_hash

with app.app_context():
    e = Employee.query.filter_by(emp_code='PCM001').first()
    if not e:
        print("!! PCM001 not found")
        sys.exit(1)

    if '--password-file' in sys.argv:
        i = sys.argv.index('--password-file')
        if i + 1 >= len(sys.argv):
            sys.exit('--password-file needs a PATH')
        with open(sys.argv[i + 1]) as fh:
            new_pw = fh.readline().rstrip('\r\n')
        if not new_pw:
            sys.exit(f'--password-file {sys.argv[i + 1]} is empty')
        # Reset password and clear must_change
        e.password_hash = generate_password_hash(new_pw)
        e.must_change_pw = False
        e.is_active = True
        db.session.commit()
        print(f"password check for the supplied password: {e.check_password(new_pw)}")
    else:
        print("No --password-file given — PCM001 password left unchanged")

    print(f"PCM001: active={e.is_active} role={e.role} must_change={e.must_change_pw}")

    print(f"\nCompanies:     {Company.query.count()}")
    print(f"Opportunities: {Opportunity.query.count()}")
    print(f"Leads:         {Lead.query.count()}")

    from sqlalchemy import func
    print("\nLead stage breakdown:")
    for stage, n in db.session.query(Lead.stage, func.count(Lead.id)).group_by(Lead.stage).all():
        print(f"  {stage:20s} {n:>6d}")

    print("\nLeads with opp_number (Opportunities-page count):",
          Lead.query.filter(Lead.opp_number.isnot(None)).count())
    print("Leads with opp_number AND stage='RFQ Generated':",
          Lead.query.filter(Lead.opp_number.isnot(None),
                            Lead.stage == 'RFQ Generated').count())
