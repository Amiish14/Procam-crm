"""
Confirm the suggestions that need no judgement.

    .venv/bin/python scripts/confirm_suggestions.py --check
    .venv/bin/python scripts/confirm_suggestions.py --apply --actor DIR12010

Only the shape `auto_link` would have taken on its own: exactly
one exact-rule match, nothing disagreeing, above the confidence
floor, not a placeholder, not blocked. Everything else stays in
the queue for a person.

It calls `account_suggest.confirm_one`, the same function behind
the button on Account check — a second implementation of "link a
lead to an account" is how the two drift until one of them stops
writing the owner row.

Every lead is recomputed at the moment it is written, not read
from the stored suggestion. The pass that produced those rows ran
earlier, and an account created since changes the answer.
"""
import argparse
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402
from app.models.assignment import (LeadAccountSuggestion  # noqa: E402
                                   as _SUG)


def candidates():
    """Leads whose standing suggestion is a single exact match."""
    rows = (_SUG.query
            .filter(_SUG.exact_single.is_(True),
                    _SUG.acted_at.is_(None),
                    _SUG.account_id.isnot(None))
            .order_by(_SUG.lead_id).all())
    out = []
    for row in rows:
        lead = db.session.get(Lead, row.lead_id)
        if lead is not None and not lead.company_id:
            out.append((lead, row))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--actor', default='DIR12010')
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()
    if args.check == args.apply:
        raise SystemExit('Choose one: --check or --apply.')

    with app.app_context():
        from app.services import account_suggest

        print(f'database: {db.engine.url}')
        print(f'MODE:     {"APPLY" if args.apply else "DRY RUN"}')
        print(f'actor:    {args.actor}\n')

        rows = candidates()
        if args.limit:
            rows = rows[:args.limit]
        print(f'  single exact matches waiting: {len(rows)}\n')

        if args.check:
            for lead, row in rows[:20]:
                print(f'    #{lead.id:<7}'
                      f'{(lead.company or "")[:26]:<28}'
                      f'-> account {row.account_id} '
                      f'({row.rule}, {row.confidence})')
            if len(rows) > 20:
                print(f'    … and {len(rows) - 20} more')
            print('\nDRY RUN — nothing written. Each one is '
                  'recomputed at write time, so a few may be '
                  'skipped on the apply; they will be listed.')
            return

        done, skipped = [], []
        for lead, _row in rows:
            ok, detail = account_suggest.confirm_one(
                lead, actor=args.actor, via='bulk pass')
            if ok:
                done.append((lead, detail))
            else:
                skipped.append((lead, detail))
        try:
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'  !! ROLLED BACK: {type(exc).__name__}: '
                  f'{str(exc)[:200]}')
            raise SystemExit(1)

        print(f'  confirmed {len(done)}, skipped {len(skipped)}\n')
        for code, n in Counter(d['owner']
                               for _l, d in done).most_common():
            print(f'    {code:<14}{n:>5} lead(s) now owned')
        if skipped:
            print('\n  skipped, with the reason — not dropped '
                  'quietly:')
            for lead, why in skipped:
                print(f'    #{lead.id:<7}'
                      f'{(lead.company or "")[:24]:<26}{why}')


if __name__ == '__main__':
    main()
