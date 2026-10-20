"""
CRM 2.0 R1 — how much of the opportunities table is already a lead.

    .venv/bin/python scripts/crm2_opportunity_overlap.py --check

READ ONLY. There is no apply: the decision is that leads carry the
one pipeline and `opportunities` becomes read-only history, and this
report exists to show what that history actually contains before
anybody relies on it.

The join is `opp_number`, not `lead_id`
    The first version of this script joined on `opportunities.lead_id`
    and reported 9,522 of 9,540 rows as orphan history with nothing
    behind them. That was wrong in the way that matters: `lead_id` is
    null on almost every row, while `opp_number` is on 9,540 of them
    and matches a lead 9,524 times. The two tables are not disjoint —
    they are the same pipeline joined by a key the first version did
    not look at.

    So both keys are tried, and the report says which one did the
    work. A bucket that depends on the join is a bucket that has to
    show its join.

Buckets:

  matched            an opportunity that reaches a lead, by either key
  opportunity-only   reaches no lead at all
  lead-only          an open lead with no opportunity row
"""
import argparse
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, Opportunity, STAGES_TERMINAL, app, db   # noqa: E402


def survey():
    by_id = {l.id: l for l in Lead.query.all()}
    by_number = {}
    for lead in by_id.values():
        num = (lead.opp_number or '').strip()
        if num:
            by_number.setdefault(num, lead)

    matched, unmatched = [], []
    reached, by_key = set(), Counter()
    for o in Opportunity.query.all():
        lead = None
        if o.lead_id and o.lead_id in by_id:
            lead, key = by_id[o.lead_id], 'lead_id'
        else:
            num = (o.opp_number or '').strip()
            if num and num in by_number:
                lead, key = by_number[num], 'opp_number'
        if lead is None:
            unmatched.append(o)
            continue
        matched.append((o, lead, key))
        by_key[key] += 1
        reached.add(lead.id)

    open_leads = Lead.query.filter(
        Lead.is_archived.isnot(True),
        ~Lead.stage.in_(STAGES_TERMINAL)).all()
    lead_only = [l for l in open_leads if l.id not in reached]

    return {'matched': matched, 'unmatched': unmatched,
            'lead_only': lead_only, 'open_leads': open_leads,
            'by_key': by_key}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true', default=True)
    ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('MODE:     READ ONLY — this script has no apply\n')
        s = survey()
        total = len(s['matched']) + len(s['unmatched'])
        print(f'  opportunities                {total}')
        print(f'    reach a lead               {len(s["matched"])}')
        for key, n in s['by_key'].most_common():
            print(f'      by {key:<22} {n}')
        print(f'    reach no lead              '
              f'{len(s["unmatched"])}')
        print(f'\n  open leads                   '
              f'{len(s["open_leads"])}')
        print(f'    with no opportunity row    {len(s["lead_only"])}')

        # Does the mirror still track? This is what decides whether
        # the table is history worth reading or a stale copy.
        same = [(o, l) for o, l, _k in s['matched']
                if (o.stage or '').lower() == (l.stage or '').lower()]
        differ = [(o, l) for o, l, _k in s['matched']
                  if (o.stage or '').lower() != (l.stage or '').lower()]
        print(f'\n  stage agreement')
        print(f'    the same                   {len(same)}')
        print(f'    different                  {len(differ)}')
        pairs = Counter((o.stage or '(null)', l.stage or '(null)')
                        for o, l in differ)
        for (os_, ls), n in pairs.most_common(8):
            print(f'      opp {os_:<20} lead {ls:<20} {n}')
        print('    Most of these are one stage under two names, not '
              'drift — the opportunities table carries its own '
              'vocabulary for the same pipeline.')

        # The part that is worth money: an estimate the lead does not
        # have, sitting on the opportunity beside it.
        gap = [(o, l) for o, l, _k in s['matched']
               if (o.value_inr or 0) > 0
               and not (l.estimated_value_inr or 0)]
        open_gap = [(o, l) for o, l in gap
                    if (l.stage or '') not in STAGES_TERMINAL
                    and not l.is_archived]
        print(f'\n  value on the opportunity, none on the lead')
        print(f'    all leads                  {len(gap)}')
        print(f'    open leads                 {len(open_gap)}')
        print('    CRM-VALUE-01 found 928 of 945 open leads with no '
              'estimate. This is where some of them are.')
        for o, l in open_gap[:8]:
            print(f'      lead #{l.id:<7} {(l.company or "")[:28]:<28} '
                  f'{o.value_inr}')

        print('\nREAD ONLY — nothing was written.')


if __name__ == '__main__':
    main()
