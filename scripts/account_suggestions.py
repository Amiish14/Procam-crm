"""
Suggest a customer for every open lead that has none.

    .venv/bin/python scripts/account_suggestions.py --check
    .venv/bin/python scripts/account_suggestions.py --apply
    .venv/bin/python scripts/account_suggestions.py --down --yes

A SUGGESTION, not a link. Nothing is attached to any lead here:
the pass writes `lead_account_suggestion` rows and the Account
check screen is where a person presses the button.

That distinction is the whole design. `auto_link` would place a
good number of these on its own, and placing them on arrival is
right — a mistake there is one lead. Applying the same rule to 352
historical leads in one go is a different act: a mistake is a
customer quietly moved to the wrong account, found months later by
somebody wondering why the history is thin.

`--down` removes the suggestions and leaves every lead exactly as
it found it, because it never touched them.
"""
import argparse
import os
import sys
from collections import Counter, defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import app, db                                   # noqa: E402
from app.models.assignment import (LeadAccountSuggestion  # noqa: E402
                                   as _SUG)

BUCKETS = ('one exact', 'several', 'weak', 'none')


def head_of(vertical):
    try:
        from app.services import vertical_heads
        found = vertical_heads.heads_for_vertical(vertical) or []
        return found[0] if found else ''
    except Exception:                                     # noqa: BLE001
        return ''


def _name(code):
    from app import Employee
    if not code:
        return ''
    emp = Employee.query.filter_by(emp_code=code).first()
    return getattr(emp, 'name', '') or code


def run(*, write=False):
    from app.services import account_gate, account_suggest

    out = []
    # An offline pass sees every lead; said explicitly, because
    # pending() refuses to widen its own scope by default.
    for lead in account_suggest.pending(account_suggest.batch_scope()):
        found = account_suggest.suggest_for(lead)
        found['vertical'] = account_gate.vertical_of(lead) \
            or '(no vertical)'
        found['bucket'] = account_suggest.bucket(found)
        if write:
            account_suggest.store(found)
        out.append(found)
    if write:
        db.session.commit()
    return out


def undo():
    rows = _SUG.query.filter(_SUG.acted_at.is_(None)).all()
    for row in rows:
        db.session.delete(row)
    db.session.commit()
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    ap.add_argument('--samples', type=int, default=12)
    args = ap.parse_args()
    if len([a for a in (args.check, args.apply, args.down) if a]) != 1:
        raise SystemExit('Choose one: --check, --apply or --down.')

    with app.app_context():
        print(f'database: {db.engine.url}')
        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            n = undo()
            print(f'  {n} unacted suggestion(s) removed. No lead was '
                  f'ever changed by this script, so there is nothing '
                  f'else to put back.')
            return

        print(f'MODE:     {"APPLY" if args.apply else "DRY RUN — nothing is written"}\n')
        rows = run(write=args.apply)
        print(f'  open leads with no account   {len(rows)}\n')

        counts = Counter(r['bucket'] for r in rows)
        for key in BUCKETS:
            print(f'    {key:<12}{counts.get(key, 0):>6}')

        print('\n  by vertical and head:')
        by_v = defaultdict(Counter)
        for r in rows:
            by_v[r['vertical']][r['bucket']] += 1
        head_cache = {}
        print(f'    {"vertical":<22}{"head":<24}'
              + ''.join(f'{k:>11}' for k in BUCKETS) + f'{"total":>8}')
        print(f'    {"-" * 92}')
        for vertical, c in sorted(by_v.items(),
                                  key=lambda kv: -sum(kv[1].values())):
            if vertical not in head_cache:
                code = head_of(vertical)
                head_cache[vertical] = (f'{code} {_name(code)}'.strip()
                                        if code else '— nobody —')
            print(f'    {vertical[:21]:<22}{head_cache[vertical][:23]:<24}'
                  + ''.join(f'{c.get(k, 0):>11}' for k in BUCKETS)
                  + f'{sum(c.values()):>8}')

        print('\n  the rules behind the suggestions:')
        for rule, n in Counter(r['rule'] or '(nothing matched)'
                               for r in rows).most_common():
            print(f'    {rule:<26}{n:>6}')

        exact = [r for r in rows if r['bucket'] == 'one exact']
        print(f'\n  {len(exact)} could be confirmed in bulk — exactly '
              f'one exact-rule match, nothing disagreeing. The rest '
              f'need a person to look.')
        for r in exact[:args.samples]:
            print(f"    #{r['lead'].id:<7}"
                  f"{(r['lead'].company or '')[:24]:<26}"
                  f"-> {r['account'][:28]:<30}{r['rule']} "
                  f"({r['confidence']})")

        several = [r for r in rows if r['bucket'] == 'several']
        if several:
            print(f'\n  {len(several)} matched more than one account '
                  f'— these are the likely duplicates, and the '
                  f'reason the queue exists:')
            for r in several[:args.samples]:
                others = ', '.join(a['name'][:20]
                                   for a in r['alternatives'][:2])
                print(f"    #{r['lead'].id:<7}"
                      f"{(r['lead'].company or '')[:22]:<24}"
                      f"{r['account'][:24]:<26} vs {others}")

        if args.check:
            print('\nDRY RUN — nothing written. `--apply` records '
                  'these as suggestions; no lead is linked by this '
                  'script at any point.')
            return
        print(f'\n  {len(rows)} suggestion(s) recorded. No lead was '
              f'linked: that happens on Account check, by a person.')


if __name__ == '__main__':
    main()
