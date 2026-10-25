"""
"Not interested" becomes a lost reason, and stops being a stage.

    .venv/bin/python scripts/2026_10_25_retire_not_interested.py --check
    .venv/bin/python scripts/2026_10_25_retire_not_interested.py --apply
    .venv/bin/python scripts/2026_10_25_retire_not_interested.py --down --yes

R2 converted 1,212 leads off that stage and gave 1,210 of them the
reason "Not interested" — a reason that is not in the lost_reason
master list, so it cannot be chosen from the dropdown and does not
appear as an option on any report filter. The option to set the
*stage* meanwhile stayed on the screen, and three more leads were
filed under it within three days.

So the code change removes the stage, and this adds the reason that
replaces it. Doing one without the other would leave people with an
outcome they can see in the data and cannot record.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import app, db                                   # noqa: E402
from app.models.master_data import MasterItem             # noqa: E402

LIST_KEY = 'lost_reason'
CODE = 'Not interested'


def _row():
    return MasterItem.query.filter_by(list_key=LIST_KEY,
                                      code=CODE).first()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()
    if len([a for a in (args.check, args.apply, args.down) if a]) != 1:
        raise SystemExit('Choose one: --check, --apply or --down.')

    with app.app_context():
        from sqlalchemy import text

        from app.services import audit, stage_map

        print(f'database: {db.engine.url}\n')
        here = _row() is not None
        print(f'  lost reason "{CODE}": '
              f'{"already there" if here else "absent"}')
        used = db.session.execute(text(
            'SELECT COUNT(*) FROM leads WHERE lost_reason = :r'),
            {'r': CODE}).scalar()
        print(f'  leads already carrying it: {used}')
        still = db.session.execute(text(
            'SELECT COUNT(*) FROM leads WHERE stage = :s'),
            {'s': 'Not Interested'}).scalar()
        print(f'  leads still on the stage:  {still}')
        print(f'  stage retired in code:     '
              f'{stage_map.retired("Not Interested") is not None}')

        if args.check:
            print('\nCHECK ONLY — nothing written.')
            return

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            row = _row()
            if row is None:
                print('  already absent')
                return
            db.session.delete(row)
            audit.record('master_item.removed', 'master_item', CODE,
                         old={'list_key': LIST_KEY, 'code': CODE},
                         actor='CRM-ACCOUNT-FIRST-01',
                         reason='retirement reverted')
            db.session.commit()
            print(f'  removed "{CODE}" from {LIST_KEY}')
            return

        if here:
            print('\n  nothing to do — the reason is already there')
            return
        db.session.add(MasterItem(
            list_key=LIST_KEY, code=CODE, label=CODE, is_active=True))
        audit.record('master_item.added', 'master_item', CODE,
                     new={'list_key': LIST_KEY, 'code': CODE},
                     actor='CRM-ACCOUNT-FIRST-01',
                     reason='replaces the retired "Not Interested" '
                            'stage; 1,210 leads already carry it')
        db.session.commit()
        print(f'\n  added "{CODE}" to {LIST_KEY}. The {used} lead(s) '
              f'already carrying it are now on a reason that exists '
              f'in the list.')


if __name__ == '__main__':
    main()
