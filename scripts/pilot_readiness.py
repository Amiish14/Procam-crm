"""
What is still missing before the pilot can run.

    .venv/bin/python scripts/pilot_readiness.py
    .venv/bin/python scripts/pilot_readiness.py --vertical Warehousing

READ-ONLY. It changes nothing and is safe to run on production
at any time.

Most of what is left is not code. A coordinator nobody has named,
an SCM table with a gap, terms still marked DRAFT, a mail switch
nobody has turned on — each fails quietly, and the person who
notices is the customer.

Exit code 1 when something is MISSING, so this can sit in a
deploy script and stop a pilot going live half-configured.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import app, db                                   # noqa: E402
from app.services import pilot_readiness as pr            # noqa: E402

MARK = {pr.OK: 'OK     ', pr.MISSING: 'MISSING', pr.WARN: 'WARN   '}


def _line(check, indent='    '):
    print(f'{indent}{MARK.get(check["state"], "?")}  '
          f'{check["key"]:<26}{check["detail"]}')
    if check['state'] != pr.OK and check.get('who'):
        print(f'{indent}         ask: {check["who"]}'
              + (f' · {check["fix"]}' if check.get('fix') else ''))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--vertical', default='')
    ap.add_argument('--quiet', action='store_true',
                    help='only the lines that are not OK')
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('READ-ONLY — nothing is changed.\n')
        data = pr.report()

        if data['no_pilot']:
            print('  No vertical has the RFQ chain switched on for '
                  'an active person, so there is nothing to be '
                  'ready for yet.')
            print('  Switch it on with scripts/set_pilot.py, then '
                  'run this again.')
            return

        print('  COMPANY-WIDE')
        for check in data['company']:
            if args.quiet and check['state'] == pr.OK:
                continue
            _line(check)

        for vertical, checks in data['verticals'].items():
            if args.vertical and vertical != args.vertical:
                continue
            people = ', '.join(sorted(data['people'][vertical]))
            print(f'\n  {vertical.upper()}  ({people})')
            for check in checks:
                if args.quiet and check['state'] == pr.OK:
                    continue
                _line(check)

        print(f'\n  {data["missing"]} missing, '
              f'{data["warnings"]} warning(s).')
        if data['ready']:
            print('  Everything the pilot needs is set.')
        else:
            print('  The pilot will run, but the MISSING lines '
                  'above fail quietly: a step opens owned by '
                  'nobody, or a mail is composed and never sent.')
            raise SystemExit(1)


if __name__ == '__main__':
    main()
