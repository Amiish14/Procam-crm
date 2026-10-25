"""
The chain's vocabularies: delay reasons, hold reasons, the rest.

    .venv/bin/python scripts/seed_rfq_chain.py --check
    .venv/bin/python scripts/seed_rfq_chain.py --apply

Master Data, so an administrator edits them without a release.
Already-present values are left exactly as they are — somebody
may have reworded one, and a seed that overwrites is a seed that
undoes people's work every time it runs.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import app, db                                   # noqa: E402
from app.services import rfq_chain as rc                  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    if args.check == args.apply:
        raise SystemExit('Choose one: --check or --apply.')

    with app.app_context():
        from app.models.master_data import MasterItem

        print(f'database: {db.engine.url}')
        print(f'MODE:     {"APPLY" if args.apply else "DRY RUN"}\n')
        for list_key, values in rc.LISTS.items():
            have = {i.code for i in MasterItem.query.filter_by(
                list_key=list_key).all()}
            missing = [v for v in values if v not in have]
            print(f'  {list_key:<28}{len(have):>3} present, '
                  f'{len(missing):>2} to add')

        if args.check:
            print('\nCHECK ONLY — nothing written.')
            return

        added, present = rc.seed_lists(actor='CRM-RFQ-CHAIN-01')
        db.session.commit()
        print(f'\n  {len(added)} value(s) written, {present} were '
              f'already there and were left alone.')
        for key in rc.LISTS:
            n = MasterItem.query.filter_by(list_key=key,
                                           is_active=True).count()
            print(f'    {key:<28}{n:>3} active')


if __name__ == '__main__':
    main()
