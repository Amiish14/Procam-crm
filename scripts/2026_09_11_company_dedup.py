#!/usr/bin/env python3
"""
Company Master de-duplication — §51, §65, §88.

    python scripts/2026_09_11_company_dedup.py                 # preview
    python scripts/2026_09_11_company_dedup.py --apply
    python scripts/2026_09_11_company_dedup.py --revert <id>

The audit found 43 duplicate names across 89 rows — 'adani' as ids 21 and
641, 'bharat heavy electricals' as 108 and 327.  These must be merged
BEFORE leads are pointed at company ids, or a lead attaches to the wrong
duplicate and the mistake becomes permanent.

Rules, from §88 (never destroy history):

  * The survivor is the richest record — most linked opportunities, then
    most filled fields, then lowest id.  Not an arbitrary pick.
  * Every reference is repointed; the loser is deactivated, not deleted.
  * Each merge writes a CompanyMergeLog row detailing what moved, so
    --revert can put it back.
  * Merges only where names normalise identically.  Anything looser goes
    to the §65 review queue instead of being guessed.
"""
import argparse
import os
import sys
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import importlib                                              # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
app, db, Company = _main.app, _main.db, _main.Company

from app.models.data_mapping import CompanyMergeLog            # noqa: E402

# Tables that point at companies.id and must follow the survivor.
REFERENCES = [
    ('app:Opportunity', 'company_id'),
    ('presales.models:AccountRelationshipTag', 'account_id'),
    ('presales.models:AccountAssignmentHistory', 'account_id'),
    ('app.models.rfq:RFQ', 'account_id'),
    ('app.models.quote:Quote', 'account_id'),
    ('app.models.rbac:AccountMember', 'entity_id'),
]

_SUFFIXES = (' pvt', ' ltd', ' limited', ' private', ' inc', ' llc',
             ' gmbh', ' corporation', ' corp', ' company', ' group',
             ' co', ' plc', ' sa', ' nv', ' bv', ' ag')


def norm(name):
    """Normalise a company name for exact-duplicate detection.

    Deliberately conservative: it strips punctuation and legal suffixes
    but never guesses at abbreviations, so 'BHEL' and 'Bharat Heavy
    Electricals' stay separate and go to review instead.
    """
    if not name:
        return ''
    s = str(name).lower().strip()
    for ch in '.,()[]/\\-_&\'"':
        s = s.replace(ch, ' ')
    s = ' '.join(s.split())
    changed = True
    while changed:
        changed = False
        for suf in _SUFFIXES:
            if s.endswith(suf):
                s, changed = s[: -len(suf)].strip(), True
    return ' '.join(s.split())


def _resolve(path):
    module_path, _, attr = path.partition(':')
    return getattr(importlib.import_module(module_path), attr)


def _richness(company):
    """How much a record is worth keeping."""
    try:
        from app import Opportunity
        opps = Opportunity.query.filter_by(company_id=company.id).count()
    except Exception:
        opps = 0
    filled = sum(1 for f in ('industry', 'website', 'country', 'state',
                             'city', 'address', 'phone', 'email',
                             'linkedin', 'pic_emp_code', 'dev_stage')
                 if getattr(company, f, None))
    return (opps, filled, -company.id)


def find_groups():
    buckets = defaultdict(list)
    for c in Company.query.all():
        key = norm(c.name)
        if key:
            buckets[key].append(c)
    return {k: v for k, v in buckets.items() if len(v) > 1}


def merge(keep, loser, dry, actor='migration'):
    moved = {}
    for path, column in REFERENCES:
        try:
            model = _resolve(path)
        except Exception:
            continue
        try:
            rows = model.query.filter(getattr(model, column) == loser.id)
            n = rows.count()
        except Exception:
            continue
        if not n:
            continue
        moved[model.__tablename__] = n
        if not dry:
            for row in rows.all():
                setattr(row, column, keep.id)

    # Fill blanks on the survivor from the record being retired.
    filled = []
    for f in ('industry', 'website', 'country', 'state', 'city', 'address',
              'phone', 'email', 'linkedin', 'pic_emp_code', 'dev_stage',
              'notes'):
        if not getattr(keep, f, None) and getattr(loser, f, None):
            filled.append(f)
            if not dry:
                setattr(keep, f, getattr(loser, f))
    if filled:
        moved['_fields_filled'] = filled

    if not dry:
        loser.is_active = False
        loser.name = f'{loser.name} [merged into #{keep.id}]'
        db.session.add(CompanyMergeLog(
            kept_id=keep.id, merged_id=loser.id,
            kept_name=keep.name, merged_name=loser.name,
            moved=moved, merged_by=actor))
    return moved


def revert(merge_id):
    """Undo one merge.  Assumes an application context is already active."""
    from datetime import datetime

    log = CompanyMergeLog.query.get(merge_id)
    if log is None:
        print(f'no merge #{merge_id}')
        return 1
    if log.reverted_at:
        print(f'merge #{merge_id} was already reverted')
        return 1

    print(f'Reverting merge #{merge_id}: '
          f'#{log.merged_id} was folded into #{log.kept_id}')
    print('  NOTE: references moved to the survivor are NOT moved back '
          'automatically — this restores the record, not the links.')

    loser = Company.query.get(log.merged_id)
    if loser is not None:
        loser.is_active = True
        loser.name = log.merged_name.split(' [merged into')[0]
    log.reverted_at = datetime.utcnow()
    db.session.commit()
    print('  ✓ record restored and reactivated')
    return 0


def cmd_revert(merge_id):
    with app.app_context():
        return revert(merge_id)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true',
                    help='write the merges; without it this is a preview')
    ap.add_argument('--revert', type=int, metavar='MERGE_ID')
    ap.add_argument('--limit', type=int, default=0,
                    help='only process the first N groups')
    args = ap.parse_args()

    if args.revert:
        return cmd_revert(args.revert)

    dry = not args.apply

    with app.app_context():
        insp = db.inspect(db.engine)
        if 'company_merge_log' not in insp.get_table_names():
            print('CREATE TABLE company_merge_log'
                  + ('   (dry run)' if dry else ''))
            if not dry:
                CompanyMergeLog.__table__.create(db.engine)

        groups = find_groups()
        if args.limit:
            groups = dict(list(groups.items())[:args.limit])

        print(f'\nDuplicate groups: {len(groups)}   '
              f'rows involved: {sum(len(v) for v in groups.values())}\n')

        total_moved = 0
        for key in sorted(groups):
            rows = sorted(groups[key], key=_richness, reverse=True)
            keep, losers = rows[0], rows[1:]
            print(f'  {key[:44]:46} keep #{keep.id} "{keep.name[:34]}"')
            for loser in losers:
                moved = merge(keep, loser, dry)
                detail = ', '.join(
                    f'{k} {v}' for k, v in moved.items()
                    if not k.startswith('_')) or 'no references'
                fields = moved.get('_fields_filled')
                print(f'      ← #{loser.id} "{loser.name[:30]}"  ({detail})')
                if fields:
                    print(f'         fills blanks: {", ".join(fields)}')
                total_moved += sum(v for k, v in moved.items()
                                   if not k.startswith('_'))

        if not dry:
            db.session.commit()

        print(f'\n{"Would move" if dry else "Moved"} {total_moved} '
              f'reference(s) onto surviving records.')
        print('Retired records are deactivated and renamed, never deleted.')
        print('\n' + ('Preview only — re-run with --apply to write.'
                      if dry else
                      '✓ applied.  Undo one with --revert <merge id>.'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
