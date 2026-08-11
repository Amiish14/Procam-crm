"""
Reclassify existing Leads + Opportunities to the approved workflow stages.

Legacy stage → New stage:
    New                → New Opportunity
    Call Done          → Qualified
    Profile Sent       → Qualified
    Appointment        → Qualified
    Visit Done         → Qualified
    RFQ Generated      → (see opp_stage) Proposal Due / Negotiation Due / Qualified
    Won                → Won
    Lost               → (see opp_stage) Lost / Not Interested / On Hold

For imported leads at 'RFQ Generated', we look at `opp_stage` (which we stored
the raw Excel value in title case for) to bucket correctly:
    Proposal Sent   → Negotiation Due   (proposal sent, waiting on customer)
    Budgetary       → Proposal Due      (budgetary quote in flight)
    Qualified       → Qualified

For imported leads at 'Lost', opp_stage disambiguates:
    Not Participated → Not Interested
    Cancelled        → On Hold
    Lost             → Lost

Idempotent — running twice is safe.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import app, db, Lead, Opportunity  # noqa: E402


# General legacy → new mapping (used when opp_stage doesn't help)
LEGACY_MAP = {
    'New':            'New Opportunity',
    'Call Done':      'Qualified',
    'Profile Sent':   'Qualified',
    'Appointment':    'Qualified',
    'Visit Done':     'Qualified',
    'RFQ Generated':  'Qualified',        # default; refined by opp_stage below
    'Won':            'Won',
    'Lost':           'Lost',
    # already-new-vocab stages: pass through
    'New Opportunity': 'New Opportunity',
    'Qualified':       'Qualified',
    'Proposal Due':    'Proposal Due',
    'Negotiation Due': 'Negotiation Due',
    'Decision Taken':  'Decision Taken',
    'On Hold':         'On Hold',
    'Not Interested':  'Not Interested',
}

# Excel opp_stage refinement
OPP_STAGE_REFINE = {
    'Won':               'Won',
    'Lost':              'Lost',
    'Cancelled':         'On Hold',
    'Not Participated':  'Not Interested',
    'Proposal Sent':     'Negotiation Due',
    'Budgetary':         'Proposal Due',
    'Qualified':         'Qualified',
    'Closed':            'Won',
}


def refined_stage(lead):
    """Compute the correct new stage for a Lead using its current stage + opp_stage."""
    legacy = (lead.stage or '').strip()
    opp_st = (lead.opp_stage or '').strip()

    # If the row has a rich opp_stage, that wins (imported leads).
    if opp_st and opp_st in OPP_STAGE_REFINE:
        return OPP_STAGE_REFINE[opp_st]

    return LEGACY_MAP.get(legacy, 'New Opportunity')


def main():
    with app.app_context():
        total = Lead.query.count()
        print(f"Reclassifying {total} leads...")

        # Collect counts before
        from sqlalchemy import func
        before = dict(db.session.query(Lead.stage, func.count(Lead.id))
                      .group_by(Lead.stage).all())

        updated = 0
        batch = 0
        for lead in Lead.query.yield_per(500):
            new_stage = refined_stage(lead)
            if new_stage != lead.stage:
                lead.stage = new_stage
                updated += 1
                batch += 1
                if batch >= 500:
                    db.session.commit()
                    batch = 0
        db.session.commit()

        # Also sync Opportunity.stage to match Lead.stage where linked by opp_number.
        print("Syncing Opportunity.stage from Lead.stage where linked...")
        opp_updated = 0
        for opp in Opportunity.query.yield_per(500):
            l = Lead.query.filter_by(opp_number=opp.opp_number).first()
            if l and l.stage != opp.stage:
                opp.stage = l.stage
                opp_updated += 1
                if opp_updated % 500 == 0:
                    db.session.commit()
        db.session.commit()

        after = dict(db.session.query(Lead.stage, func.count(Lead.id))
                     .group_by(Lead.stage).all())

        print(f"\n─── DONE ────────────────────────────────────")
        print(f"  Leads updated:         {updated}")
        print(f"  Opportunities synced:  {opp_updated}")
        print(f"\n  Before:")
        for s, n in sorted(before.items(), key=lambda x: -x[1]):
            print(f"    {s:20s} {n:>6d}")
        print(f"\n  After:")
        for s, n in sorted(after.items(), key=lambda x: -x[1]):
            print(f"    {s:20s} {n:>6d}")


if __name__ == '__main__':
    main()
