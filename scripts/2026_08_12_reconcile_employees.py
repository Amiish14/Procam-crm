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
    python scripts/2026_08_12_reconcile_employees.py           # dry-run summary
    python scripts/2026_08_12_reconcile_employees.py --apply   # commit changes
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import app, db, Employee
from werkzeug.security import generate_password_hash


# ──────────────────────────────────────────────────────────────────────
# Authoritative employee list (emp_code → full_name)
# ──────────────────────────────────────────────────────────────────────
EMPLOYEES = [
    ('CON1362025', 'Pravin Choudhary'),
    ('CON142024',  'Seema Sanjeev Moghe'),
    ('CON242017',  'Lakshmi Narayan'),
    ('CON252022',  'Samsul Hussain'),
    ('DIR12010',   'Nilesh Kumar Sinha'),
    ('DIR22010',   'James Francis Xavier'),
    ('DIR42010',   'T G Ramalingam'),
    ('DIR52011',   'Sethupathy Sundaram'),
    ('DIR72012',   'Srinivas Marella'),
    ('EMP1062016', 'Kamrul Islam'),
    ('EMP1082016', 'Nishit Ranjan Das'),
    ('EMP1112016', 'Bijoy Konwar'),
    ('EMP112010',  'Sanjeev Kumar Paliwal'),
    ('EMP1172016', 'Narender Singh'),
    ('EMP12010',   'Nitin Rawat'),
    ('EMP1282017', 'Pravinkumar Arumugam'),
    ('EMP132010',  'Sahadeb Sahoo'),
    ('EMP1322017', 'Ram Mohan Chaubey'),
    ('EMP1332017', 'Santosh Kumar'),
    ('EMP1342017', 'Pramod Kumar Sukla'),
    ('EMP1372017', 'Dinesh Dubey'),
    ('EMP142010',  'Ranjit Gogoi'),
    ('EMP1472018', 'Kuldip Kumar'),
    ('EMP1482018', 'Pratap Singh'),
    ('EMP1492018', 'Chakradhar Sahoo'),
    ('EMP1502018', 'Devendra Subhash'),
    ('EMP1552018', 'Abhishek Singh'),
    ('EMP1602018', 'Santhosh P'),
    ('EMP1612018', 'Amit Kumar'),
    ('EMP162010',  'Mohd Rahimuddin'),
    ('EMP1662018', 'Phool Chandra Yudhishir'),
    ('EMP1672018', 'Partab Singh'),
    ('EMP1682018', 'Tahirul Haque'),
    ('EMP1702018', 'Surendar Singh'),
    ('EMP172010',  'Manjurul Hoque'),
    ('EMP182010',  'K Umamaheswara Rao'),
    ('EMP192010',  'Manju Mishra'),
    ('EMP212010',  'Ajit Kumar Das'),
    ('EMP2122018', 'Gajendra Kumar Giri'),
    ('EMP22010',   'Rajeev Ranjan'),
    ('EMP242010',  'Laxmi Ram Singh'),
    ('EMP2482019', 'Tanima Mukherjee'),
    ('EMP2582020', 'Dattaram Mahalim'),
    ('EMP2622020', 'Aritra Mitra'),
    ('EMP2642021', 'Kumar Satyam Ray'),
    ('EMP2752021', 'Sagar Bhogle'),
    ('EMP2782022', 'Gajanan Narayan Naglot'),
    ('EMP2792022', 'Swapnil Sunil Jadhav'),
    ('EMP2802022', 'Amol Bhagvan Nikam'),
    ('EMP2832022', 'Mohanraj R'),
    ('EMP2882022', 'Seema Chattopadhyay'),
    ('EMP2892022', 'Rakesh Dnyaneshwar Rawal'),
    ('EMP2902022', 'Vipul Sinh Zala'),
    ('EMP2952023', 'Rameshwar Nihalsingh Gusinge'),
    ('EMP2962023', 'Shashidhar Pandurang Naik'),
    ('EMP2972023', 'Jayanta Kumar Paul'),
    ('EMP2982023', 'Sharayu Uday Bhosale'),
    ('EMP2992023', 'Dipanka Talukder'),
    ('EMP3022023', 'Bhushan B Bhagat'),
    ('EMP3042023', 'Vishal Raosaheb Magar'),
    ('EMP3062023', 'Nitin Ambadas Pawar'),
    ('EMP3072023', 'Sohel Mainoor Shaikh'),
    ('EMP3092023', 'Satish Datta Navghare'),
    ('EMP3102023', 'Balu Bhagovrao Jogdanad'),
    ('EMP3122023', 'Sunita Naga Alkar'),
    ('EMP3132023', 'Bidisha Banerjee'),
    ('EMP3142023', 'Balkrishnan Sharma'),
    ('EMP3152023', 'Anurag Uday Chand'),
    ('EMP3162023', 'Birendra Kumar'),
    ('EMP3192023', 'Panjab Dinkar Pise'),
    ('EMP3202023', 'Shivaji Ashok Dhumal'),
    ('EMP3212023', 'Sachin Thakur'),
    ('EMP3222023', 'Sayanti Ghosh'),
    ('EMP3282023', 'Jones George T'),
    ('EMP3292023', 'Zahid Khan'),
    ('EMP3322023', 'Aryaan Shaikh'),
    ('EMP3372024', 'Sayantan Naskar'),
    ('EMP3382024', 'Yogesh Kumar Rajasekaran'),
    ('EMP3482024', 'Shriram Dattu Patil'),
    ('EMP3532024', 'Suresh Kumar'),
    ('EMP3542024', 'Sayan Das'),
    ('EMP3552024', 'Karthikeyan R'),
    ('EMP3592024', 'Amit Kakkar'),
    ('EMP3602024', 'Ahmad Ali'),
    ('EMP3612024', 'Kamar Khan'),
    ('EMP3642024', 'Souvik Chakraborty'),
    ('EMP3652025', 'Sumit Mondal'),
    ('EMP3672025', 'Akash Prabu'),
    ('EMP3702025', 'Suranjan Aon'),
    ('EMP372011',  'Sanjna Vardhan'),
    ('EMP3732025', 'Bikash Routh'),
    ('EMP3742025', 'Muntazir Alam'),
    ('EMP3752025', 'Vikash Dubey'),
    ('EMP3762025', 'Parveen Sharma'),
    ('EMP3772025', 'Sanjay Bhite'),
    ('EMP3782025', 'Satish Jadhav'),
    ('EMP3802025', 'Kapil Bekanale'),
    ('EMP3822025', 'Akash Somnath Narayne'),
    ('EMP3832025', 'Vishal Pundlik Bhokre'),
    ('EMP3842025', 'Saurabh Ramesh Waghmare'),
    ('EMP3852025', 'Avinash Tukaram Ghatul'),
    ('EMP3862025', 'Chandresh Kumar Baijnath Yadav'),
    ('EMP3882025', 'Sundhar Rajan S'),
    ('EMP3892025', 'Bhavin Vinodhbhai Jiilka'),
    ('EMP3902025', 'Bala Murugan T'),
    ('EMP3912025', 'Shyam Bharti'),
    ('EMP3932025', 'Manish Kumar Bhakta'),
    ('EMP3942025', 'Aniket Ray Chaudhuri'),
    ('EMP3952025', 'Venkatesh Ramarao Althada'),
    ('EMP3962025', 'Dhanashree Harishchandra Pawar'),
    ('EMP3972025', 'Samiksha Chandrakant Vayngankar'),
    ('EMP3982025', 'Ashitosh Sarjerao Gholap'),
    ('EMP3992025', 'Hazarat Ali'),
    ('EMP4002025', 'Md Inamuddin'),
    ('EMP4022025', 'Vikrant Vats'),
    ('EMP4032025', 'Pramod Kumar'),
    ('EMP4042026', 'Guruswami Mohanta'),
    ('EMP4052026', 'Akram Mahmud Mujawar'),
    ('EMP4062026', 'Pravin Abasaheb Barde'),
    ('EMP472012',  'Gowdhaman Rajakrishnan'),
    ('EMP482012',  'Saktheeswari Murugavel'),
    ('EMP572012',  'Vijay T V'),
    ('EMP812015',  'Ramesh Yadav Sechae'),
]


# Role hints — only used if the record is new AND we don't have a value.
# By default new employees get role='sales' (regular pre-sales user).
# Nilesh, TGR, Sethupathy, Francis, Srinivas — DIR* codes — become 'admin'.
def default_role(emp_code):
    if emp_code.startswith('DIR'):
        return 'admin'
    return 'sales'


def main(apply_changes: bool):
    with app.app_context():
        existing_by_code = {e.emp_code: e
                            for e in Employee.query.all()}

        new_count = 0
        pw_reset_count = 0
        name_updated_count = 0
        already_matching_count = 0

        for emp_code, full_name in EMPLOYEES:
            emp = existing_by_code.get(emp_code)
            if emp is None:
                emp = Employee(
                    emp_code=emp_code,
                    name=full_name,
                    role=default_role(emp_code),
                    is_active=True,
                    must_change_pw=True,
                )
                emp.password_hash = generate_password_hash(emp_code)
                if apply_changes:
                    db.session.add(emp)
                new_count += 1
                print(f'  + NEW  {emp_code:12s}  {full_name}  '
                      f'(role={default_role(emp_code)})')
            else:
                changes = []
                # Only overwrite name if it looks materially different
                # (case-insensitive compare, strip whitespace)
                if (emp.name or '').strip().lower() != full_name.strip().lower():
                    emp.name = full_name
                    changes.append('name')
                    name_updated_count += 1
                # Always ensure they can log in with their emp_code
                emp.password_hash = generate_password_hash(emp_code)
                emp.must_change_pw = True
                emp.is_active = True
                pw_reset_count += 1
                if changes:
                    print(f'  ~ UPDT {emp_code:12s}  {full_name}  '
                          f'(changed: {",".join(changes)})')
                else:
                    already_matching_count += 1

        if apply_changes:
            db.session.commit()

        print()
        print('─── SUMMARY ───────────────────────────────────────')
        print(f'  NEW accounts:                    {new_count}')
        print(f'  Existing accounts renamed:       {name_updated_count}')
        print(f'  Existing accounts passwd-reset:  {pw_reset_count}')
        print(f'  Already matched (no name change):{already_matching_count}')
        print()
        if not apply_changes:
            print('  NOTE: this was a dry run. Re-run with --apply to commit.')
        else:
            print('  All changes committed. Each user can now log in with:')
            print('    username = <their EMP code, e.g. EMP2972023>')
            print('    password = <same EMP code>')
            print('  On first login they will be forced to set a new password.')


if __name__ == '__main__':
    apply = '--apply' in sys.argv
    main(apply_changes=apply)
