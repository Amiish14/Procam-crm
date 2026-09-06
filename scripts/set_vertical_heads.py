#!/usr/bin/env python3
"""
Show and set the vertical heads that gate report visibility.

Reports are visible to admins (whole company) and to vertical heads
(their own vertical only — see app/reports_v2/routes.py::_scope).  With
no heads flagged, only admins can see anything.

    # who is a head today, and who could be
    python scripts/set_vertical_heads.py --list

    # preview, then apply
    python scripts/set_vertical_heads.py --head EMP372011 --head EMP572012
    python scripts/set_vertical_heads.py --head EMP372011 --apply

    # take the flag away
    python scripts/set_vertical_heads.py --clear EMP372011 --apply

Nothing is written without --apply.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import importlib                                            # noqa: E402
_main = importlib.import_module('app')
app, db, Employee = _main.app, _main.db, _main.Employee

# Words that usually mark someone senior enough to head a vertical.
_SENIOR = ('head', 'director', 'vp', 'vice president', 'chief', 'gm',
           'general manager', 'agm', 'dgm', 'manager', 'lead', 'president')


def _is_senior(emp):
    text = f'{emp.designation or ""} {emp.department or ""}'.lower()
    return any(w in text for w in _SENIOR)


def cmd_list():
    verticals = {}
    for e in Employee.query.filter_by(is_active=True).all():
        verticals.setdefault((e.vertical or '').strip() or '(no vertical)',
                             []).append(e)

    heads = [e for e in Employee.query.filter_by(is_active=True,
                                                 is_vertical_head=True).all()]
    admins = Employee.query.filter_by(is_active=True, role='admin').all()

    print(f'ADMINS ({len(admins)}) — see the whole company')
    for e in sorted(admins, key=lambda x: x.emp_code):
        print(f'    {e.emp_code:12} {e.name}')

    print(f'\nVERTICAL HEADS ({len(heads)}) — see only their own vertical')
    if not heads:
        print('    (none — only admins can open reports right now)')
    for e in sorted(heads, key=lambda x: x.emp_code):
        v = (e.vertical or '').strip()
        warn = '   ⚠ no vertical set — would see only themselves' if not v else ''
        print(f'    {e.emp_code:12} {e.name:32} {v}{warn}')

    print('\nVERTICALS')
    for v in sorted(verticals):
        people = verticals[v]
        cur = [e for e in people if e.is_vertical_head]
        print(f'\n  {v}   ({len(people)} people)')
        if cur:
            for e in cur:
                print(f'      HEAD  {e.emp_code:12} {e.name}')
        else:
            print('      HEAD  — none —')
            cands = [e for e in people
                     if _is_senior(e) and not e.is_vertical_head]
            for e in sorted(cands, key=lambda x: x.name)[:6]:
                print(f'      cand. {e.emp_code:12} {e.name:32}'
                      f' {e.designation or ""}')
            if not cands:
                print('      (no obvious candidate by designation)')

    print('\nTo grant:  python scripts/set_vertical_heads.py '
          '--head <EMP_CODE> [--head ...] --apply')


def cmd_change(codes, value, apply):
    verb = 'grant' if value else 'revoke'
    found, missing = [], []
    for c in codes:
        e = Employee.query.filter_by(emp_code=c.strip().upper()).first()
        (found.append(e) if e else missing.append(c))

    for c in missing:
        print(f'  !! no employee with emp_code {c}')

    if not found:
        print('nothing to do')
        return 1 if missing else 0

    print(f'Would {verb} vertical-head on {len(found)}:' if not apply
          else f'{verb.title()}ing vertical-head on {len(found)}:')
    for e in found:
        v = (e.vertical or '').strip()
        note = ''
        if value and not v:
            note = '   ⚠ no vertical set — they would see only themselves'
        if value and not e.is_active:
            note += '   ⚠ inactive'
        print(f'    {e.emp_code:12} {e.name:32} {v or "(no vertical)"}{note}')
        if apply:
            e.is_vertical_head = value

    if apply:
        db.session.commit()
        print(f'\n✓ committed — {len(found)} updated')
    else:
        print('\n(dry run — re-run with --apply to write)')
    return 1 if missing else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--list', action='store_true',
                    help='show admins, heads, verticals and candidates')
    ap.add_argument('--head', action='append', default=[], metavar='EMP_CODE',
                    help='mark as a vertical head (repeatable)')
    ap.add_argument('--clear', action='append', default=[], metavar='EMP_CODE',
                    help='remove the vertical-head flag (repeatable)')
    ap.add_argument('--apply', action='store_true',
                    help='actually write; without it this is a dry run')
    args = ap.parse_args()

    with app.app_context():
        if args.head or args.clear:
            rc = 0
            if args.head:
                rc |= cmd_change(args.head, True, args.apply)
            if args.clear:
                rc |= cmd_change(args.clear, False, args.apply)
            return rc
        cmd_list()
        return 0


if __name__ == '__main__':
    sys.exit(main())
