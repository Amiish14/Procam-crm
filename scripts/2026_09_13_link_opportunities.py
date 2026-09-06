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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args()
    dry = args.check

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
        index = build_index(
            Company.query.filter(Company.is_active.is_(True)).all())

        stats = Counter()
        for opp in orphans:
            if opp.lead_id and opp.lead_id in lead_company:
                stats['from parent lead'] += 1
                if not dry:
                    opp.company_id = lead_company[opp.lead_id]
                continue

            raw = lead_name.get(opp.lead_id) if opp.lead_id else None
            raw = raw or (opp.title or '')
            company, reason, _cands = match(raw, index) if raw else (
                None, 'no_match', [])
            if company is not None:
                stats['by name match'] += 1
                if not dry:
                    opp.company_id = company.id
            else:
                stats['still unlinked'] += 1

        if not dry:
            db.session.commit()

        print(f'\n  {"Would link" if dry else "Linked"} from parent lead   '
              f'{stats["from parent lead"]:>7}')
        print(f'  {"Would link" if dry else "Linked"} by name match     '
              f'{stats["by name match"]:>7}')
        print(f'  cannot be determined            '
              f'{stats["still unlinked"]:>7}')

        if dry:
            projected = before_visible + stats['from parent lead'] \
                        + stats['by name match']
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
