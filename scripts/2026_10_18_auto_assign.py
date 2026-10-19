"""
Auto-assignment: four tables, and the replacement map seeded.

    sender_account_link     what corrections have taught the matcher
    account_vertical_owner  who serves an account for which work
    owner_replacement       where a departed person's leads go
    owner_confirmation      an owner a rule chose, awaiting a person

All additive. No lead, account or contact is written.

The replacement map is seeded from the list Nilesh gave on 18
October. It is a table and not code because it changes whenever
somebody leaves, and a deploy is the wrong unit for that.

    .venv/bin/python scripts/2026_10_18_auto_assign.py --check
    .venv/bin/python scripts/2026_10_18_auto_assign.py
    .venv/bin/python scripts/2026_10_18_auto_assign.py --down --yes
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect                            # noqa: E402

from app import Employee, app, db                         # noqa: E402
from app.models.assignment import (AccountVerticalOwner,  # noqa: E402
                                   OwnerConfirmation, OwnerReplacement,
                                   SenderAccountLink)

TABLES = (('sender_account_link', SenderAccountLink),
          ('account_vertical_owner', AccountVerticalOwner),
          ('owner_replacement', OwnerReplacement),
          ('owner_confirmation', OwnerConfirmation))

#: Who takes over from whom. Named people, because that is how the
#: decision was given; resolved to codes at migration time so a
#: misspelling fails loudly here rather than silently later.
REPLACEMENTS = (
    ('Manju Mishra', 'Sanjna Vardhan'),
    ('Venkatesh Althada', 'Vijay T V'),
    ('Sumit Mondal', 'EMP4092026'),
    ('Dhanashree Pawar', 'EMP4092026'),
    ('EMP1552018', 'Seema Chattopadhyay'),
)

#: Named in the original decision and dropped on 18 October: none of
#: them is on the employee master and none holds a lead, so there is
#: nothing for a replacement row to do. Kept here rather than
#: deleted, so that somebody who remembers the names can see they
#: were considered and why they are absent.
#:
#:     Suketu Bhupesh Thakkar, Manas Ganguly, Setu Prassan,
#:     Laxmi Yadav
#:
#: Add them through Admin → Replacements if they turn up under a
#: spelling the CRM holds.


def _find(who):
    """An employee by code or by name. (code, label, problem)."""
    probe = (who or '').strip()
    if not probe:
        return None, probe, 'blank'
    emp = Employee.query.filter_by(emp_code=probe.upper()).first()
    if emp is not None:
        return emp.emp_code, f'{emp.emp_code} {emp.name}', (
            '' if emp.is_active else 'NOT ACTIVE')
    rows = Employee.query.filter(Employee.name.ilike(f'%{probe}%')).all()
    if not rows:
        # "Venkatesh Althada" is on file as "Venkatesh Ramarao
        # Althada". A contiguous substring misses every person whose
        # middle name somebody left out, which is most of them.
        wanted = {w for w in probe.lower().split() if len(w) > 1}
        rows = [e for e in Employee.query.all()
                if wanted and wanted <= set((e.name or '').lower().split())]
    if len(rows) == 1:
        emp = rows[0]
        return emp.emp_code, f'{emp.emp_code} {emp.name}', (
            '' if emp.is_active else 'NOT ACTIVE')
    if not rows:
        return None, probe, 'NO SUCH EMPLOYEE'
    return None, probe, f'AMBIGUOUS — {len(rows)} matches: ' + ', '.join(
        f'{r.emp_code} {r.name}' for r in rows[:4])


def seed_replacements(dry):
    print('\n  replacement map:')
    written, refused = 0, 0
    for who, to_whom in REPLACEMENTS:
        from_code, from_label, from_problem = _find(who)
        to_code, to_label, to_problem = _find(to_whom)

        # The person leaving may legitimately be inactive — that is
        # the point. The replacement may not be.
        bad = []
        if from_code is None:
            bad.append(f'from: {from_problem}')
        if to_code is None:
            bad.append(f'to: {to_problem}')
        elif to_problem == 'NOT ACTIVE':
            bad.append('to: that person is not active either')
        if bad:
            refused += 1
            print(f'      REFUSED {who} -> {to_whom}: {"; ".join(bad)}')
            continue

        existing = OwnerReplacement.query.filter_by(
            from_emp_code=from_code).first()
        state = ('already there' if existing
                 and existing.to_emp_code == to_code else
                 ('repointed' if existing else 'new'))
        print(f'      {from_label:<34} -> {to_label:<34} {state}'
              + ('   (from is still active)' if from_problem != 'NOT ACTIVE'
                 else ''))
        if dry:
            continue
        if existing is None:
            existing = OwnerReplacement(from_emp_code=from_code)
            db.session.add(existing)
        existing.to_emp_code = to_code
        existing.active = True
        existing.updated_by = 'migration'
        existing.note = 'CRM-AUTO-ASSIGN-01, decided 18 Oct 2026'
        written += 1
    if not dry:
        db.session.commit()
        print(f'      + {written} row(s), {refused} refused')
    return refused


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        if not Employee.query.count():
            raise SystemExit('Refusing to run: not the production database.')
        insp = inspect(db.engine)

        if args.down:
            if args.check or not args.yes:
                for name, _m in TABLES:
                    print(f'== DRY-RUN == WOULD drop '
                          f'{name if insp.has_table(name) else "(absent) " + name}')
                print('  sender_account_link holds what people have taught '
                      'the matcher by correcting it. Dropping it loses '
                      'that and the matcher goes back to its old rate.')
                if not args.yes:
                    print('  Refusing without --yes.')
                return
            for name, model in TABLES:
                if insp.has_table(name):
                    model.__table__.drop(bind=db.engine)
                    print(f'  - {name}')
            return

        for name, model in TABLES:
            if insp.has_table(name):
                print(f'  . {name} already there')
            elif args.check:
                print(f'  WOULD create {name}')
            else:
                db.metadata.create_all(bind=db.engine,
                                       tables=[model.__table__])
                print(f'  + {name}')

        refused = seed_replacements(dry=args.check)

        if args.check:
            print('\n  Nothing else is seeded. The account-vertical owners '
                  'are seeded by their own script, after a dry run.')
            print('DRY RUN — nothing written.')
            return
        if refused:
            print(f'\n  {refused} replacement(s) were refused — see above. '
                  f'Fix the names and re-run; the rest are in place.')
        print('\nDone. FEATURE_AUTO_ASSIGN is still off; nothing changes '
              'until it is switched on.')


if __name__ == '__main__':
    main()
