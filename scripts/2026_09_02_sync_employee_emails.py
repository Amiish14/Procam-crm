#!/usr/bin/env python
"""
v2026-09-02 — Set each CRM employee's official email from the supplied list.

The list in data/employee_emails_2026_09_02.txt is the authoritative source
of official addresses. This walks every employee in the CRM and finds the
address that belongs to them, deriving a probable name from each address's
local part and fuzzy-matching it against Employee.name:

    rp.shah@procamgroup.in          -> "rp shah"
    sahadeb.sahoo2012@gmail.com     -> "sahadeb sahoo"
    PRAVIN.CHOUDHARY@PROCAMGROUP.IN -> "pravin choudhary"

Output is employee-centric, one row per employee, showing what they have
now and what the official list says:

    SAME    already correct
    NEW     no email on file, one found            (written by --apply)
    CHANGE  on file differs from the official list (needs --overwrite too)
    LOW     no confident match — map it by hand with --map

Nothing is written without --apply, and a match below --threshold is never
applied: a wrong address means someone else's leads land in the wrong inbox.

Examples:
    python scripts/2026_09_02_sync_employee_emails.py                    # preview
    python scripts/2026_09_02_sync_employee_emails.py --apply
    python scripts/2026_09_02_sync_employee_emails.py --apply --overwrite
    python scripts/2026_09_02_sync_employee_emails.py --map PCM042=zahid.khan@procamgroup.in --apply
"""
import argparse
import os
import re
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

DEFAULT_LIST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'employee_emails_2026_09_02.txt')

# Words that appear in local parts but never in a person's name.
_NOISE = re.compile(r'(procam|myprocam|logistics|group|mail|admin)', re.I)


def name_from_email(addr: str) -> str:
    local = addr.split('@', 1)[0].lower()
    local = re.sub(r'\d+', ' ', local)
    local = re.sub(r'[._\-+]+', ' ', local)
    local = _NOISE.sub(' ', local)
    return re.sub(r'\s+', ' ', local).strip()


def load_addresses(path: str) -> list:
    out, seen = [], set()
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#') or '@' not in line:
                continue
            if line.lower() not in seen:
                seen.add(line.lower())
                out.append(line)
    return out


def score(a: str, b: str) -> float:
    try:
        from rapidfuzz import fuzz
        return max(fuzz.token_set_ratio(a, b), fuzz.partial_ratio(a, b))
    except ImportError:
        from difflib import SequenceMatcher
        return SequenceMatcher(None, a, b).ratio() * 100


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true', help='write the matches')
    ap.add_argument('--overwrite', action='store_true',
                    help='also replace an email that differs from the list')
    ap.add_argument('--include-inactive', action='store_true',
                    help='also process deactivated employees')
    ap.add_argument('--file', default=DEFAULT_LIST)
    ap.add_argument('--threshold', type=float, default=80.0)
    ap.add_argument('--map', action='append', default=[],
                    metavar='EMPCODE=EMAIL', help='force a mapping; repeatable')
    args = ap.parse_args()

    addresses = load_addresses(args.file)
    guesses = [(a, name_from_email(a)) for a in addresses]

    forced = {}
    for pair in args.map:
        if '=' not in pair:
            sys.exit(f'--map needs EMPCODE=EMAIL, got {pair!r}')
        code, addr = pair.split('=', 1)
        forced[code.strip().upper()] = addr.strip()

    with app.app_context():
        q = Employee.query
        if not args.include_inactive:
            q = q.filter_by(is_active=True)
        emps = q.order_by(Employee.emp_code).all()

        print(f'official addresses : {len(addresses)}')
        print(f'employees in scope : {len(emps)}'
              f'   (active {Employee.query.filter_by(is_active=True).count()}'
              f', total {Employee.query.count()})')
        if not args.apply:
            print('\nPREVIEW — nothing will be written.\n')

        out = Counter()
        used = set()
        rows, low = [], []

        for emp in emps:
            cur = (emp.email or '').strip()
            code = emp.emp_code.upper()

            if code in forced:
                best, sc = forced[code], 100.0
            else:
                ename = (emp.name or '').lower().strip()
                best, sc = None, 0.0
                for addr, guess in guesses:
                    if addr.lower() in used or not guess:
                        continue
                    v = score(guess, ename)
                    if v > sc:
                        best, sc = addr, v

            if best is None or sc < args.threshold:
                low.append((emp, cur, best, sc))
                out['LOW — needs manual map'] += 1
                continue

            used.add(best.lower())
            if cur.lower() == best.lower():
                verdict = 'SAME'
            elif not cur:
                verdict = 'NEW'
            else:
                verdict = 'CHANGE'
            out[verdict] += 1
            rows.append((emp, cur, best, sc, verdict))

            if args.apply and (verdict == 'NEW'
                               or (verdict == 'CHANGE' and args.overwrite)):
                emp.email = best

        if args.apply:
            db.session.commit()

        print('%-11s %-24s %-32s %-32s %5s  %s'
              % ('emp_code', 'name', 'current email', 'official email', 'score', ''))
        print('-' * 120)
        for emp, cur, best, sc, verdict in rows:
            print('%-11s %-24s %-32s %-32s %5.1f  %s' % (
                emp.emp_code, (emp.name or '')[:24], (cur or '—')[:32],
                best[:32], sc, verdict))

        if low:
            print('\n--- NO CONFIDENT MATCH (nothing written) ---')
            print('%-11s %-24s %-32s %-28s %s'
                  % ('emp_code', 'name', 'current email', 'closest address', 'score'))
            print('-' * 108)
            for emp, cur, best, sc in low:
                print('%-11s %-24s %-32s %-28s %.1f' % (
                    emp.emp_code, (emp.name or '')[:24], (cur or '—')[:32],
                    (best or '—')[:28], sc))

        leftover = [a for a in addresses if a.lower() not in used]
        if leftover:
            print(f'\n--- {len(leftover)} addresses not assigned to anyone ---')
            print('(people not in the CRM, or deactivated — try --include-inactive)')
            for a in leftover:
                print('  ' + a)

        print('\n--- summary ---')
        for k, n in out.most_common():
            print(f'  {n:>5}  {k}')
        if not args.apply:
            print('\nPreview only.')
            print('  --apply              write the NEW ones')
            print('  --apply --overwrite  also correct the CHANGE ones')
            print('  --map PCM042=addr@x  fix a LOW row by hand')


if __name__ == '__main__':
    main()
