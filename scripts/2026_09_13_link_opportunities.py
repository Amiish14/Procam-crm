#!/usr/bin/env python3
"""
Link Opportunities to Company Master — §7, §64, §67.

    python scripts/2026_09_13_link_opportunities.py --check
    python scripts/2026_09_13_link_opportunities.py

The audit's sharpest finding: 1,348 opportunities are Won, but only 835
reach the Won Value by Account report.  513 are skipped because they have
no company_id — the report is faithfully reporting data that was never
linked.

4,269 opportunities have no company at all.  Most were raised from a lead,
and that lead now has a company_id, so the opportunity's company follows
by inheritance.  That is not a guess: an opportunity belongs to the
company of the lead it came from.

Three passes, strongest evidence first:

  1. inherit from the parent lead's company_id
  2. match the parent lead's company NAME against Company Master
  3. anything left is reported, never guessed

Additive and reversible — only NULL company_id values are ever written.
"""
import argparse
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import importlib                                              # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
app, db = _main.app, _main.db
Lead, Opportunity, Company = _main.Lead, _main.Opportunity, _main.Company

from app.services.company_match import build_index, match      # noqa: E402


def report_gap():
    """What the Won Value report can currently see, and what it cannot."""
    won = Opportunity.query.filter(Opportunity.stage == 'Won')
    total = won.count()
    visible = won.filter(Opportunity.company_id.isnot(None)).count()
    valued = won.filter(Opportunity.company_id.isnot(None),
                        Opportunity.value_inr.isnot(None)).count()
    return total, visible, valued


def diagnose():
    """Why an opportunity cannot be attributed.

    The first run linked only 3 of 4,269, which contradicts 94.7% of leads
    being linked — so the assumption that these hang off leads is wrong.
    This reports what they actually carry.
    """
    orphans = Opportunity.query.filter(
        Opportunity.company_id.is_(None)).all()
    print(f'\nOpportunities with no company: {len(orphans)}\n')

    with_lead = [o for o in orphans if o.lead_id]
    print(f'  have a lead_id                {len(with_lead):>7}')
    print(f'  no lead_id at all             '
          f'{len(orphans) - len(with_lead):>7}')

    if with_lead:
        lead_ids = [o.lead_id for o in with_lead]
        found = {l.id: l for l in Lead.query.filter(
            Lead.id.in_(lead_ids)).all()}
        missing = [o for o in with_lead if o.lead_id not in found]
        linked = [o for o in with_lead
                  if o.lead_id in found and found[o.lead_id].company_id]
        print(f'    …lead row missing            {len(missing):>7}')
        print(f'    …lead has a company          {len(linked):>7}')
        print(f'    …lead also unlinked          '
              f'{len(with_lead) - len(missing) - len(linked):>7}')

    titled = [o for o in orphans if (o.title or '').strip()]
    print(f'\n  have a title                  {len(titled):>7}')
    owned = [o for o in orphans if (o.owner_emp_code or '').strip()]
    print(f'  have an owner                 {len(owned):>7}')
    staged = Counter((o.stage or '(blank)') for o in orphans)
    print('\n  by stage:')
    for stage, n in staged.most_common(8):
        print(f'    {stage:28} {n:>7}')

    print('\n  sample of what they carry:')
    for o in orphans[:8]:
        print(f'    #{o.id:<6} {(o.opp_number or "—")[:16]:18}'
              f' lead={o.lead_id or "—":<8}'
              f' title={(o.title or "—")[:40]}')
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--diagnose', action='store_true',
                    help='explain why opportunities cannot be attributed')
    args = ap.parse_args()
    dry = args.check

    if args.diagnose:
        with app.app_context():
            return diagnose()

    with app.app_context():
        before_total, before_visible, before_valued = report_gap()
        print('Won Value by Account — before')
        print(f'  Won opportunities in the database   {before_total:>7}')
        print(f'  reaching the report                 {before_visible:>7}'
              f'   ({before_visible / before_total * 100:.1f}%)'
              if before_total else '')
        print(f'  with a value figure                 {before_valued:>7}')

        orphans = Opportunity.query.filter(
            Opportunity.company_id.is_(None)).all()
        print(f'\nOpportunities with no company: {len(orphans)}')

        lead_company = dict(
            db.session.query(Lead.id, Lead.company_id)
            .filter(Lead.company_id.isnot(None)).all())
        lead_name = dict(db.session.query(Lead.id, Lead.company).all())

        # The 2023 import dropped lead_id but kept opp_number on BOTH
        # sides, and Lead carries the same column.  That shared reference
        # is the join the import left behind — and it is a real link, not
        # an inference: the two rows describe one opportunity.
        by_opp_number = {}
        for lead_id, number, company_id in db.session.query(
                Lead.id, Lead.opp_number, Lead.company_id).filter(
                Lead.opp_number.isnot(None),
                Lead.opp_number != '').all():
            key = (number or '').strip()
            if not key:
                continue
            by_opp_number.setdefault(key, set()).add(company_id)

        # Only usable where every lead sharing that number agrees on the
        # company.  Where they disagree the number is ambiguous and must
        # not be resolved by picking one.
        opp_number_company = {
            k: next(iter(v)) for k, v in by_opp_number.items()
            if len(v) == 1 and next(iter(v)) is not None}
        conflicted = sum(1 for v in by_opp_number.values() if len(v) > 1)

        index = build_index(
            Company.query.filter(Company.is_active.is_(True)).all())

        stats = Counter()
        for opp in orphans:
            is_won = (opp.stage or '') == 'Won'

            if opp.lead_id and opp.lead_id in lead_company:
                stats['from parent lead'] += 1
                if is_won:
                    stats['won_recovered'] += 1
                if not dry:
                    opp.company_id = lead_company[opp.lead_id]
                continue

            key = (opp.opp_number or '').strip()
            if key and key in opp_number_company:
                stats['by shared opp_number'] += 1
                if is_won:
                    stats['won_recovered'] += 1
                if not dry:
                    opp.company_id = opp_number_company[key]
                continue
            if key and key in by_opp_number:
                stats['opp_number ambiguous'] += 1
                continue

            raw = lead_name.get(opp.lead_id) if opp.lead_id else None
            raw = raw or (opp.title or '')
            company, reason, _cands = match(raw, index) if raw else (
                None, 'no_match', [])
            if company is not None:
                stats['by name match'] += 1
                if is_won:
                    stats['won_recovered'] += 1
                if not dry:
                    opp.company_id = company.id
            else:
                stats['still unlinked'] += 1
                if is_won:
                    stats['won_still_unlinked'] += 1

        if conflicted:
            print(f'\n  {conflicted} opp_number(s) map to more than one '
                  f'company — left alone rather than picked between.')

        if not dry:
            db.session.commit()

        verb = 'Would link' if dry else 'Linked'
        print(f'\n  {verb} from parent lead       '
              f'{stats["from parent lead"]:>7}')
        print(f'  {verb} by shared opp_number {stats["by shared opp_number"]:>7}')
        print(f'  {verb} by name match        {stats["by name match"]:>7}')
        print(f'  opp_number ambiguous            '
              f'{stats["opp_number ambiguous"]:>7}')
        print(f'  cannot be determined            '
              f'{stats["still unlinked"]:>7}')

        if dry:
            # Only Won rows affect the Won Value report; the earlier
            # version added every linked opportunity to a Won-only
            # baseline and printed more Won deals than exist.
            projected = before_visible + stats['won_recovered']
            pct = (projected / before_total * 100) if before_total else 0
            print(f'\nWon Value by Account would then see '
                  f'{projected} of {before_total} Won deals ({pct:.1f}%)'
                  f' — up from {before_visible}.')
            if stats['won_still_unlinked']:
                print(f'  {stats["won_still_unlinked"]} Won deal(s) would '
                      f'remain unattributed and keep showing on the '
                      f'"not linked to an account" line.')
            print(f'\nProjection only — re-run without --check to apply.')
        else:
            after_total, after_visible, after_valued = report_gap()
            print('\nWon Value by Account — after')
            print(f'  Won opportunities                   {after_total:>7}')
            print(f'  reaching the report                 {after_visible:>7}'
                  f'   ({after_visible / after_total * 100:.1f}%)'
                  if after_total else '')
            print(f'  recovered by this migration         '
                  f'{after_visible - before_visible:>7}')

            missing_value = after_visible - after_valued
            if missing_value:
                print(f'\n  {missing_value} linked Won opportunit'
                      f'{"y" if missing_value == 1 else "ies"} still have no '
                      f'value_inr and contribute 0 to the total.')
                print('  That is a data-entry gap, not a linking one — the '
                      'value was never recorded.')

            print('\n✓ applied.  Only NULL company_id values were written, '
                  'so nothing already linked was changed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
