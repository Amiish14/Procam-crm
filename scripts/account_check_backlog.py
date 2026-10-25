"""
What each vertical head would find in Account check on day one.

    .venv/bin/python scripts/account_check_backlog.py
    .venv/bin/python scripts/account_check_backlog.py --by-owner

READ-ONLY. Nothing here writes.

The queue is not evenly shared, and a head who learns the size of
their share on the morning the flag goes on will clear it by
clicking rather than by deciding — which puts wrong owners on real
customers and is worse than the gap it closes.

Two reasons a lead is held, and they need different people:

    no account          a matching question — the sender could not
                        be placed, or several accounts matched
    no owner            a staffing question — the account exists and
                        nobody is named for that vertical

The vertical is read the way the gate reads it, through
`account_gate.vertical_of`, so these counts are the queue and not
an approximation of it.
"""
import argparse
import os
import sys
from collections import Counter, defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402

UNSET = '(no vertical on the lead)'


def survey():
    """{vertical: {'no account': n, 'no owner': n, 'leads': [...]}}"""
    from app.services import account_gate, sales_rules

    out = defaultdict(lambda: {'no account': 0, 'no owner': 0,
                               'leads': []})
    rows = (Lead.query
            .filter(Lead.is_archived.isnot(True),
                    Lead.stage.in_(sales_rules.open_stages()))
            .all())
    for lead in rows:
        why = account_gate.why_pending(lead)
        if not why:
            continue
        vertical = account_gate.vertical_of(lead) or UNSET
        # startswith, not equality: the placeholder reason reads
        # 'no account — "Unknown" is a placeholder…', and matching
        # exactly filed 159 of them under "no owner", which sent
        # a staffing question to a head who actually has a
        # matching problem.
        bucket = ('no account' if why.startswith('no account')
                  else 'no owner')
        out[vertical][bucket] += 1
        out[vertical]['leads'].append(lead)
    return out, len(rows)


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--by-owner', action='store_true',
                    help='also break each vertical down by the '
                         'lead\'s current owner')
    args = ap.parse_args()

    with app.app_context():
        from app.services import account_gate

        print(f'database: {db.engine.url}')
        print('READ-ONLY — nothing is written.\n')
        if not account_gate.enabled():
            print('  (FEATURE_ACCOUNT_FIRST is off. These are the '
                  'leads it WOULD hold, counted by the same rule '
                  'the gate uses.)\n')

        by_vertical, open_total = survey()
        held = sum(v['no account'] + v['no owner']
                   for v in by_vertical.values())
        print(f'  open leads                 {open_total}')
        print(f'  would be held              {held}'
              f'  ({held * 100 // max(open_total, 1)}%)\n')

        print(f'  {"vertical":<24}{"head":<22}{"no account":>11}'
              f'{"no owner":>10}{"total":>8}')
        print(f'  {"-" * 73}')
        rows = sorted(by_vertical.items(),
                      key=lambda kv: -(kv[1]['no account']
                                       + kv[1]['no owner']))
        for vertical, counts in rows:
            total = counts['no account'] + counts['no owner']
            head = head_of(vertical) if vertical != UNSET else ''
            label = f'{head} {_name(head)}'.strip() if head \
                else '— nobody —'
            print(f'  {vertical[:23]:<24}{label[:21]:<22}'
                  f'{counts["no account"]:>11}{counts["no owner"]:>10}'
                  f'{total:>8}')

        print(f'\n  {"":<46}{"":>11}{"":>10}{held:>8}')

        if args.by_owner:
            print('\n  who holds them today:')
            for vertical, counts in rows:
                print(f'\n    {vertical}')
                for code, n in Counter(
                        (l.assigned_to or '(unassigned)')
                        for l in counts['leads']).most_common(10):
                    print(f'      {code:<14}{_name(code)[:24]:<26}'
                          f'{n:>5}')

        unset = by_vertical.get(UNSET)
        if unset:
            n = unset['no account'] + unset['no owner']
            print(f'\n  {n} lead(s) carry no vertical at all, so no '
                  f'head owns their share. They need a vertical '
                  f'before the queue can route them to anybody.')


if __name__ == '__main__':
    main()
