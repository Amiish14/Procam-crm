"""
One list of lost reasons, in Master Data.

    .venv/bin/python scripts/2026_10_25_reconcile_lost_reasons.py --check
    .venv/bin/python scripts/2026_10_25_reconcile_lost_reasons.py --apply
    .venv/bin/python scripts/2026_10_25_reconcile_lost_reasons.py --down --yes

There were two. `app.LOST_REASONS` held twelve values and fed the
dashboard filters; the `lost_reason` Master Data list held ten
different ones and filled the dropdown a person actually uses. So
"No Response" could be reported on and not chosen, and "Price too
high" could be chosen and not reported on. 1,213 leads carry "Not
interested", which was in neither.

Master Data becomes the source. This seeds it with the union of
both lists plus every reason already written on a lead, so that
nothing in the data is unsayable in the form — the state that
makes somebody pick a reason that is not true because the right
one is missing.

Nothing is renamed and no lead is touched. Reasons already in the
list keep their wording.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import text                               # noqa: E402

from app import app, db                                   # noqa: E402
from app.models.master_data import MasterItem             # noqa: E402

LIST_KEY = 'lost_reason'
ACTION = 'master_item.added'
NOTE = 'reconciled from app.LOST_REASONS and the leads in use'


def existing():
    return {(i.code or '').strip(): i
            for i in MasterItem.query.filter_by(list_key=LIST_KEY).all()}


def in_use():
    """Every distinct lost_reason actually written on a lead.

    Free-text reasons ("Others: duplicate of #123", "Restored —
    …") are deliberately excluded: they are one-off sentences, not
    vocabulary, and putting 800 of them in a dropdown would make it
    unusable.
    """
    rows = db.session.execute(text(
        "SELECT lost_reason, COUNT(*) FROM leads "
        "WHERE lost_reason IS NOT NULL AND TRIM(lost_reason) <> '' "
        "GROUP BY lost_reason ORDER BY COUNT(*) DESC")).fetchall()
    #: What marks a sentence rather than a category. Both dashes:
    #: "Others – Duplicate of lead #12115" uses an en dash and
    #: slipped past a filter that only knew the em dash, which
    #: would have put a single lead's reference in the dropdown.
    marks = (':', '—', '–', '#')
    out = []
    for reason, n in rows:
        reason = (reason or '').strip()
        if len(reason) > 40 or any(m in reason for m in marks):
            continue
        out.append((reason, n))
    return out


def plan():
    from app import LOST_REASONS_SEED as LOST_REASONS

    have = existing()
    used = dict(in_use())
    wanted = []
    for code in list(LOST_REASONS) + [r for r, _n in in_use()]:
        code = (code or '').strip()
        if code and code not in have and code not in [
                c for c, _w in wanted]:
            wanted.append((code, used.get(code, 0)))
    return have, wanted, used


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
        from app.services import audit

        print(f'database: {db.engine.url}\n')
        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            rows = MasterItem.query.filter_by(
                list_key=LIST_KEY, description=NOTE).all()
            for row in rows:
                db.session.delete(row)
            db.session.commit()
            print(f'  {len(rows)} reconciled reason(s) removed; the '
                  f'originals stay')
            return

        have, wanted, used = plan()
        print(f'  in Master Data today       {len(have)}')
        print(f'  to add                     {len(wanted)}')
        for code, n in wanted:
            tag = f'{n} lead(s) already use it' if n else 'not in use'
            print(f'      {code:<34}{tag}')

        print('\n  reasons on leads, and whether a person can pick '
              'them:')
        for code, n in sorted(used.items(), key=lambda kv: -kv[1]):
            ok = code in have or code in [c for c, _w in wanted]
            print(f'      {code:<34}{n:>6}  '
                  f'{"yes" if ok else "NO — unsayable in the form"}')

        if args.check:
            print('\nCHECK ONLY — nothing written.')
            return

        for code, _n in wanted:
            db.session.add(MasterItem(
                list_key=LIST_KEY, code=code, label=code,
                description=NOTE, is_active=True))
            audit.record(ACTION, 'master_item', code,
                         new={'list_key': LIST_KEY, 'code': code},
                         actor='CRM-ACCOUNT-FIRST-01',
                         reason='one list of lost reasons; Master '
                                'Data is the source')
        db.session.commit()
        print(f'\n  {len(wanted)} reason(s) added. Master Data is now '
              f'the source; app.LOST_REASONS reads from it.')


if __name__ == '__main__':
    main()
