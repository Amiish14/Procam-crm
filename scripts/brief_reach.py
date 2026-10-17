"""
Who does not get the 08:30 brief, and why.

The brief reaches 27–28 people out of 131. That is either correct —
most of the company does not own leads — or it is a hundred people
who cannot be reached and nobody has noticed. The two look identical
from the outside, because a report that was never sent leaves no row.

Read-only. Writes nothing, queues nothing, sends nothing. It builds
each person's board exactly as the 08:30 job would and then throws it
away.

    .venv/bin/python scripts/brief_reach.py
    .venv/bin/python scripts/brief_reach.py --csv /tmp/brief_reach.csv
    .venv/bin/python scripts/brief_reach.py --reachable-only

Every active employee is placed in exactly one bucket:

    receives            on the list, has an address, board not empty
    empty board         owns records, but nothing needs action today
                        — correct, and the commonest answer
    owns nothing        no lead and no account names them, so the job
                        never considers them
    no address          on the list, but no email on file
    bad address         an address CRM mail cannot reach
    muted               turned the brief off in their own preferences
    report off          email switched off in their preferences
"""
import argparse
import csv
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# The repository root only. Putting `scripts/` on the path as well
# makes `scripts/email_ingest.py` shadow the `email_ingest` package
# that app.py imports, and nothing loads at all.
sys.path.insert(0, os.path.dirname(_HERE))

from app import Employee, app, db                         # noqa: E402

RECEIVES = 'receives'
EMPTY = 'empty board'
NOTHING = 'owns nothing'
NO_ADDR = 'no address'
BAD_ADDR = 'bad address'
MUTED = 'muted the brief'
EMAIL_OFF = 'email switched off'

ORDER = (RECEIVES, EMPTY, NOTHING, NO_ADDR, BAD_ADDR, MUTED, EMAIL_OFF)

#: The buckets that mean "cannot be reached", as opposed to "has
#: nothing to say". This is the distinction the whole script exists
#: for.
UNREACHABLE = (NO_ADDR, BAD_ADDR)


def _owners():
    """Employee codes that any lead or account names."""
    from app import Company, Lead
    codes = set()
    for col in (Lead.assigned_to, Lead.secondary_owner,
                Company.pic_emp_code, Company.secondary_pic_emp_code):
        try:
            codes.update(c for (c,) in db.session.query(col).distinct() if c)
        except Exception:
            db.session.rollback()
    return {c.strip().upper() for c in codes if (c or '').strip()}


def classify(emp, owners):
    """(bucket, detail) for one active employee."""
    from app.services import employee_email, notify_prefs

    code = emp.emp_code
    address = (emp.email or '').strip()

    if code not in owners:
        return NOTHING, 'no lead or account names them'

    if not address:
        return NO_ADDR, 'no email address on the employee record'
    verdict = employee_email.check(address, emp_code=code, ignore_id=emp.id)
    if not verdict['ok']:
        return BAD_ADDR, ' '.join(verdict['errors'])[:160]

    prefs = notify_prefs.for_user(code)
    if not prefs.get('email_enabled', True):
        return EMAIL_OFF, 'turned email off in their notification settings'
    if not prefs.get('daily_brief', True):
        return MUTED, 'turned the daily brief off'

    # The expensive one, last: build the board the job would build.
    try:
        from app.access import scope as sc_mod
        from app.services import digests
        report = digests.build('daily', code)
    except Exception as exc:                              # noqa: BLE001
        db.session.rollback()
        return EMPTY, f'board could not be built: {str(exc)[:100]}'

    if report.get('empty'):
        return EMPTY, 'nothing on their board this morning'
    return RECEIVES, f'{report.get("total", 0)} item(s) on their board'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', help='also write the full table here')
    ap.add_argument('--reachable-only', action='store_true',
                    help='only list the people who cannot be reached')
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        owners = _owners()
        people = (Employee.query
                  .filter(Employee.is_active.is_(True))
                  .order_by(Employee.emp_code).all())
        print(f'{len(people)} active employees, {len(owners)} of whom are '
              f'named on a lead or an account\n')

        rows, tally = [], {k: 0 for k in ORDER}
        for emp in people:
            bucket, detail = classify(emp, owners)
            tally[bucket] = tally.get(bucket, 0) + 1
            rows.append({'emp_code': emp.emp_code,
                         'name': (emp.name or '').strip(),
                         'vertical': (emp.vertical or '').strip(),
                         'email': (emp.email or '').strip(),
                         'bucket': bucket, 'detail': detail})

        print('=' * 72)
        print('SUMMARY')
        print('=' * 72)
        for key in ORDER:
            flag = '  <<< cannot be reached' if key in UNREACHABLE else ''
            print(f'  {key:<22} {tally.get(key, 0):>5}{flag}')
        unreachable = sum(tally.get(k, 0) for k in UNREACHABLE)
        quiet = tally.get(EMPTY, 0) + tally.get(NOTHING, 0)
        print(f'\n  {tally.get(RECEIVES, 0)} receive it. '
              f'{quiet} genuinely have nothing to say. '
              f'{unreachable} cannot be reached at all.')
        if unreachable:
            print('  The last number is the one to act on — those people '
                  'would get the brief if their address worked.')

        print('\n' + '=' * 72)
        print('THE PEOPLE' + (' WHO CANNOT BE REACHED' if args.reachable_only
                              else ' WHO DO NOT RECEIVE IT'))
        print('=' * 72)
        if args.reachable_only:
            shown = [r for r in rows if r['bucket'] in UNREACHABLE]
        else:
            shown = [r for r in rows if r['bucket'] != RECEIVES]
        if not shown:
            print('  (none)')
        for r in sorted(shown, key=lambda r: (r['bucket'], r['emp_code'])):
            print(f'  {r["bucket"]:<22} {r["emp_code"]:<12} '
                  f'{r["name"][:26]:<26} {r["detail"][:60]}')

        if args.csv:
            with open(args.csv, 'w', newline='', encoding='utf-8') as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
            print(f'\n  full table written to {args.csv} ({len(rows)} rows)')

        print('\nRead-only: nothing was written, queued or sent.')


if __name__ == '__main__':
    main()
