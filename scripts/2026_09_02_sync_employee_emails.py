#!/usr/bin/env python
"""
v2026-09-02 — Match supplied email addresses to Employee rows and fill in
Employee.email.

The list arrived as a bare column of addresses with no names attached, so
this derives a probable name from each address's local part and fuzzy-matches
it against Employee.name:

    rp.shah@procamgroup.in          -> "rp shah"
    sahadeb.sahoo2012@gmail.com     -> "sahadeb sahoo"
    partabsingh143procam@gmail.com  -> "partabsingh"

Nothing is written without --apply, and only matches at or above --threshold
are applied. Everything below it is listed as UNMATCHED for you to map by
hand with --map, so a wrong address never lands on the wrong person — that
would send someone else's leads to them.

Examples:
    python scripts/2026_09_02_sync_employee_emails.py                  # preview
    python scripts/2026_09_02_sync_employee_emails.py --apply
    python scripts/2026_09_02_sync_employee_emails.py --threshold 90
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

# Noise that appears in local parts but never in a person's name.
_NOISE = re.compile(r'(procam|myprocam|logistics|group|mail|admin)', re.I)


def name_from_email(addr: str) -> str:
    """Best-effort person name from an address local part."""
    local = addr.split('@', 1)[0].lower()
    local = re.sub(r'\d+', ' ', local)              # sahoo2012 -> sahoo
    local = re.sub(r'[._\-+]+', ' ', local)         # rp.shah   -> rp shah
    local = _NOISE.sub(' ', local)                  # drop company words
    return re.sub(r'\s+', ' ', local).strip()


def load_addresses(path: str) -> list:
    out, seen = [], set()
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#') or '@' not in line:
                continue
            low = line.lower()
            if low not in seen:
                seen.add(low)
                out.append(line)
    return out


def score(a: str, b: str) -> float:
    """0-100 similarity. Uses rapidfuzz when available (it is in
    requirements.txt), else difflib so the script still runs anywhere."""
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
    ap.add_argument('--file', default=DEFAULT_LIST, help='address list')
    ap.add_argument('--threshold', type=float, default=85.0,
                    help='minimum match score to apply (default 85)')
    ap.add_argument('--map', action='append', default=[],
                    metavar='EMPCODE=EMAIL',
                    help='force a mapping; repeatable')
    ap.add_argument('--overwrite', action='store_true',
                    help='replace an email an employee already has')
    args = ap.parse_args()

    addresses = load_addresses(args.file)
    forced = {}
    for pair in args.map:
        if '=' not in pair:
            sys.exit(f'--map needs EMPCODE=EMAIL, got {pair!r}')
        code, addr = pair.split('=', 1)
        forced[code.strip().upper()] = addr.strip()

    with app.app_context():
        emps = Employee.query.filter_by(is_active=True).order_by(Employee.emp_code).all()
        by_code = {e.emp_code.upper(): e for e in emps}
        taken = {(e.email or '').strip().lower() for e in emps if e.email}

        print(f'addresses : {len(addresses)}   active employees: {len(emps)}')
        if not args.apply:
            print('\nPREVIEW — nothing will be written.\n')

        out = Counter()
        claimed = set()

        # 1. Forced mappings win outright.
        for code, addr in forced.items():
            emp = by_code.get(code)
            if not emp:
                print(f'  !! no active employee with code {code}')
                out['bad --map code'] += 1
                continue
            print(f'  FORCED  {code:<10} {emp.name[:28]:<28} <- {addr}')
            claimed.add(addr.lower())
            out['forced'] += 1
            if args.apply:
                emp.email = addr

        # 2. Fuzzy-match the rest.
        print('\n%-38s %-10s %-26s %5s' % ('address', 'emp_code', 'employee name', 'score'))
        print('-' * 84)
        unmatched = []
        for addr in addresses:
            if addr.lower() in claimed:
                continue
            if addr.lower() in taken:
                out['already on an employee'] += 1
                continue
            guess = name_from_email(addr)
            if not guess:
                unmatched.append((addr, None, 0))
                continue
            best, best_score = None, 0.0
            for emp in emps:
                if emp.email and not args.overwrite:
                    continue                      # already has one; leave it
                sc = score(guess, (emp.name or '').lower())
                if sc > best_score:
                    best, best_score = emp, sc
            if best is None or best_score < args.threshold:
                unmatched.append((addr, best, best_score))
                continue
            print('%-38s %-10s %-26s %5.1f' % (
                addr[:38], best.emp_code, (best.name or '')[:26], best_score))
            out['matched'] += 1
            if args.apply:
                best.email = addr
                taken.add(addr.lower())

        if args.apply:
            db.session.commit()

        if unmatched:
            print('\n--- UNMATCHED (nothing written for these) ---')
            print('%-38s %-26s %5s' % ('address', 'closest name', 'score'))
            print('-' * 72)
            for addr, best, sc in unmatched:
                print('%-38s %-26s %5.1f' % (
                    addr[:38], (best.name if best else '-')[:26], sc))
            out['unmatched'] += len(unmatched)

        print('\n--- summary ---')
        for k, n in out.most_common():
            print(f'  {n:>5}  {k}')
        missing = [e for e in emps if not e.email]
        print(f'  {len(missing):>5}  active employees still without an email')
        if not args.apply:
            print('\nPreview only. Add --apply to write the matched ones.')
            print('Map the unmatched by hand, e.g.:')
            print('  --map PCM042=zahid.khan@procamgroup.in --apply')


if __name__ == '__main__':
    main()
