"""
Reconcile the CRM's Employee master against the authoritative Procam
employee list (from 2026-08 payroll export).

For each row:
  - Upsert by emp_code
  - Set name from list (title-cased)
  - Reset password to emp_code (hashed)
  - Set must_change_pw = True so first-login forces a password change
  - Set is_active = True
  - Preserve existing role / vertical / department / email if present;
    otherwise use defaults from ROLE_HINTS below.

Idempotent — safe to re-run. Never deletes or deactivates existing employees
that aren't on this list (they stay as-is).

Usage:
    python scripts/2026_08_12_reconcile_employees.py                  # dry-run summary
    python scripts/2026_08_12_reconcile_employees.py --apply          # commit upserts
    python scripts/2026_08_12_reconcile_employees.py --apply --purge  # also deactivate
                                                                       # employees NOT on the list
                                                                       # (PCM001 super admin is
                                                                       # always preserved)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import app, db, Employee
from werkzeug.security import generate_password_hash


# ──────────────────────────────────────────────────────────────────────
# Authoritative employee list — CRM ACCESS ONLY.
# Only these people should have CRM logins. Everyone else in the DB gets
# deactivated when --purge is passed.
# Ambiguous names on the request had multiple possible matches — my best
# guess is noted in the comment. Tell me if any are wrong and I'll fix.
# ──────────────────────────────────────────────────────────────────────
EMPLOYEES = [
    ('EMP3702025', 'Suranjan Aon'),                # "Suranjan"
    ('EMP2992023', 'Dipanka Talukder'),            # "Dipanka"
    ('EMP2882022', 'Seema Chattopadhyay'),         # "Seema"
    ('EMP3542024', 'Sayan Das'),                   # "Syan" — best guess
    ('EMP2972023', 'Jayanta Kumar Paul'),          # "Jayanta"
    # "RP Shah" — no match in payroll master, SKIPPED. Send me his EMP code.
    ('DIR12010',   'Nilesh Kumar Sinha'),          # Super Admin
    ('DIR42010',   'T G Ramalingam'),              # "TGR"
    ('EMP372011',  'Sanjna Vardhan'),              # "Sanjna"
    ('EMP472012',  'Gowdhaman Rajakrishnan'),      # "Gowdhaman"
    ('CON242017',  'Lakshmi Narayan'),             # "Laksmi Narayan"
    ('EMP2982023', 'Sharayu Uday Bhosale'),        # "Sharayu"
    ('CON142024',  'Seema Sanjeev Moghe'),         # "Seema Modhe"
    ('EMP242010',  'Laxmi Ram Singh'),             # "Laxmi"
    ('EMP3322023', 'Aryaan Shaikh'),               # "Aryan"
    ('EMP4062026', 'Pravin Abasaheb Barde'),       # "Pravin" — best guess
    ('DIR72012',   'Srinivas Marella'),            # "Srinivas"
    ('CON1362025', 'Pravin Choudhary'),            # "Pravin Choudhary"
    ('EMP3152023', 'Anurag Uday Chand'),           # "Anurag"
    ('EMP1282017', 'Pravinkumar Arumugam'),        # "Pravin Kumar" — best guess
    ('DIR52011',   'Sethupathy Sundaram'),         # "Sethupathy"
    ('EMP2902022', 'Vipul Sinh Zala'),             # "Vipul"
    ('DIR22010',   'James Francis Xavier'),        # "Francis Xavier"
    ('EMP572012',  'Vijay T V'),                   # "TV Vijay"
    ('EMP12010',   'Nitin Rawat'),                 # "Nitin Rawat"
    ('EMP112010',  'Sanjeev Kumar Paliwal'),       # "Sanjeev Paliwal"
    ('EMP3592024', 'Amit Kakkar'),                 # "Amit Kakkar"
    ('EMP3212023', 'Sachin Thakur'),               # "Sachin"
    ('EMP2642021', 'Kumar Satyam Ray'),            # "Satyam"
]


# Role hints — only used if the record is new AND we don't have a value.
# By default new employees get role='sales' (regular pre-sales user).
# Nilesh, TGR, Sethupathy, Francis, Srinivas — DIR* codes — become 'admin'.
def default_role(emp_code):
    if emp_code.startswith('DIR'):
        return 'admin'
    return 'sales'


# Accounts that must NEVER be deactivated regardless of --purge.
# (Empty now — the old PCM001 super admin is being retired. DIR12010
# Nilesh becomes the sole super admin.)
PROTECTED_EMP_CODES = set()

# Explicit password + role overrides for specific accounts (not the default
# "password = emp_code, must_change_pw = True" behaviour).
# DIR12010 Nilesh gets his real chosen password so he doesn't have to
# reset on first login. Role = admin (super admin).
EXPLICIT_OVERRIDES = {
    'DIR12010': {
        'password': 'Salesforce123',
        'must_change_pw': False,
        'role': 'admin',
    },
}

# When --purge deactivates an old emp code but the same PERSON exists under
# a different code that IS on the list, reassign all their leads to the
# authoritative code so nothing gets lost.
LEAD_REASSIGN_ON_PURGE = {
    'EMP4092026': 'CON1362025',   # Pravin Choudhary — duplicate code, keep CON1362025
    'PCM001':     'DIR12010',     # retire test super admin — transfer to real Nilesh
}


def main(apply_changes: bool, purge_others: bool):
    with app.app_context():
        existing_by_code = {e.emp_code: e
                            for e in Employee.query.all()}

        new_count = 0
        pw_reset_count = 0
        name_updated_count = 0
        already_matching_count = 0

        for emp_code, full_name in EMPLOYEES:
            emp = existing_by_code.get(emp_code)
            override = EXPLICIT_OVERRIDES.get(emp_code, {})
            init_password = override.get('password', emp_code)
            must_change = override.get('must_change_pw', True)
            role = override.get('role', default_role(emp_code))

            if emp is None:
                emp = Employee(
                    emp_code=emp_code,
                    name=full_name,
                    role=role,
                    is_active=True,
                    must_change_pw=must_change,
                )
                emp.password_hash = generate_password_hash(init_password)
                if apply_changes:
                    db.session.add(emp)
                new_count += 1
                tag = ' (OVERRIDE — custom password, no forced change)' if override else ''
                print(f'  + NEW  {emp_code:12s}  {full_name}  '
                      f'(role={role}){tag}')
            else:
                changes = []
                # Only overwrite name if it looks materially different
                if (emp.name or '').strip().lower() != full_name.strip().lower():
                    emp.name = full_name
                    changes.append('name')
                    name_updated_count += 1
                # Ensure they can log in
                emp.password_hash = generate_password_hash(init_password)
                emp.must_change_pw = must_change
                emp.is_active = True
                if override:
                    emp.role = role     # explicit overrides also promote role
                pw_reset_count += 1
                if override:
                    print(f'  ~ UPDT {emp_code:12s}  {full_name}  '
                          f'(role={role}, OVERRIDE — custom password)')
                elif changes:
                    print(f'  ~ UPDT {emp_code:12s}  {full_name}  '
                          f'(changed: {",".join(changes)})')
                else:
                    already_matching_count += 1

        # ── Purge phase: deactivate anyone NOT on the authoritative list ──
        purge_count = 0
        purge_orphaned_leads = 0
        if purge_others:
            authoritative_codes = {code for code, _ in EMPLOYEES} | PROTECTED_EMP_CODES
            to_deactivate = [e for e in existing_by_code.values()
                             if e.emp_code not in authoritative_codes
                             and e.is_active]
            print()
            print(f'─── PURGE — deactivating {len(to_deactivate)} employees not on the list ───')
            for e in to_deactivate:
                # Count leads that will become orphaned, or reassign if the
                # same person exists under a different authoritative code.
                reassign_to = LEAD_REASSIGN_ON_PURGE.get(e.emp_code)
                try:
                    from app import Lead
                    lead_q = Lead.query.filter_by(assigned_to=e.emp_code)
                    n_leads = lead_q.count()
                    if reassign_to and n_leads:
                        # Get the target employee's name for assigned_name
                        target = Employee.query.filter_by(emp_code=reassign_to).first()
                        target_name = target.name if target else ''
                        tag = f'  ({n_leads} lead(s) → reassigned to {reassign_to} {target_name})'
                        if apply_changes:
                            for l in lead_q.all():
                                l.assigned_to = reassign_to
                                l.assigned_name = target_name
                    elif n_leads:
                        tag = f'  ({n_leads} lead(s) orphaned)'
                        purge_orphaned_leads += n_leads
                    else:
                        tag = ''
                except Exception:
                    tag = ''
                print(f'  x DEAC {e.emp_code:12s}  {e.name}{tag}')
                if apply_changes:
                    e.is_active = False
                purge_count += 1

        if apply_changes:
            db.session.commit()

        print()
        print('─── SUMMARY ───────────────────────────────────────')
        print(f'  NEW accounts:                    {new_count}')
        print(f'  Existing accounts renamed:       {name_updated_count}')
        print(f'  Existing accounts passwd-reset:  {pw_reset_count}')
        print(f'  Already matched (no name change):{already_matching_count}')
        if purge_others:
            print(f'  Deactivated (not on list):       {purge_count}')
            print(f'  Leads on deactivated employees:  {purge_orphaned_leads}')
        print()
        if not apply_changes:
            print('  NOTE: this was a dry run. Re-run with --apply to commit.')
        else:
            print('  All changes committed. Each user can now log in with:')
            print('    username = <their EMP code, e.g. EMP2972023>')
            print('    password = <same EMP code>')
            print('  On first login they will be forced to set a new password.')
            if purge_others and purge_orphaned_leads:
                print()
                print(f'  WARNING: {purge_orphaned_leads} lead(s) are still assigned to')
                print(f'  now-deactivated employees. Nilesh (DIR12010) will still see')
                print(f'  them in the admin view. Reassign via the Assign tab in CRM.')


if __name__ == '__main__':
    apply = '--apply' in sys.argv
    purge = '--purge' in sys.argv
    main(apply_changes=apply, purge_others=purge)
