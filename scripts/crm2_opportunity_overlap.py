"""
CRM 2.0 R1 — how much of the opportunities table is already a lead.

    .venv/bin/python scripts/crm2_opportunity_overlap.py --check

READ ONLY. There is no apply: the decision is that leads carry the
one pipeline and `opportunities` becomes read-only history, and this
report exists to show what that history actually contains before
anybody relies on it.

Three buckets:

  matched            the opportunity points at a lead that exists
  opportunity-only   no lead_id, or a lead_id that points nowhere —
                     history with nothing behind it
  lead-only          an open lead with no opportunity row

The third bucket is the interesting one. If it is large, the
opportunities table was never the pipeline and reading it as history
would under-count; if it is small, the two have been kept in step and
the table is worth keeping visible.
"""
import argparse
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, Opportunity, STAGES_TERMINAL, app, db   # noqa: E402


def survey():
    lead_ids = {r[0] for r in db.session.query(Lead.id).all()}
    opp_lead_ids = set()
    matched, orphan, headless = [], [], []

    for o in Opportunity.query.all():
        if o.lead_id and o.lead_id in lead_ids:
            matched.append(o)
            opp_lead_ids.add(o.lead_id)
        elif o.lead_id:
            orphan.append(o)        # points at a lead that is gone
        else:
            headless.append(o)      # never pointed at one

    open_leads = Lead.query.filter(
        Lead.is_archived.isnot(True),
        ~Lead.stage.in_(STAGES_TERMINAL)).all()
    lead_only = [l for l in open_leads if l.id not in opp_lead_ids]

    return {'matched': matched, 'orphan': orphan, 'headless': headless,
            'lead_only': lead_only, 'open_leads': open_leads}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true', default=True)
    ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('MODE:     READ ONLY — this script has no apply\n')
        s = survey()
        total = (len(s['matched']) + len(s['orphan'])
                 + len(s['headless']))
        print(f'  opportunities                {total}')
        print(f'    matched to a live lead     {len(s["matched"])}')
        print(f'    lead_id points nowhere     {len(s["orphan"])}')
        print(f'    no lead_id at all          {len(s["headless"])}')
        print(f'\n  open leads                   '
              f'{len(s["open_leads"])}')
        print(f'    with no opportunity row    {len(s["lead_only"])}')

        print('\n  opportunity stages:')
        for stage, n in Counter(
                (o.stage or '(null)') for o in
                s['matched'] + s['orphan'] + s['headless']
        ).most_common(10):
            print(f'    {stage:<28} {n}')

        if s['matched']:
            print('\n  where a matched pair disagrees on the stage:')
            rows = [(o, next((l for l in s['open_leads']
                              if l.id == o.lead_id), None))
                    for o in s['matched']]
            clash = [(o, l) for o, l in rows
                     if l is not None and (o.stage or '') and
                     (o.stage or '').lower() != (l.stage or '').lower()]
            print(f'    {len(clash)} of {len([r for r in rows if r[1]])} '
                  f'open matched pairs')
            for o, l in clash[:10]:
                print(f'      {o.opp_number:<16} opp={o.stage:<20} '
                      f'lead #{l.id}={l.stage}')

        print('\nREAD ONLY — nothing was written.')


if __name__ == '__main__':
    main()
