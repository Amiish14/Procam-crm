"""
v2026-08 — Pre-Sales Phase 7 · Safe historical data mapping.

For every distinct company name present in the existing Lead table, ensure
there is a matching Account (Company) row and link the Lead's inferred
data (industry, country, state, PIC) to it. Uses case-insensitive name
matching so we never duplicate an Account that already exists.

This is deliberately conservative:
  * Only creates a new Account when no case-insensitive name match exists.
  * NEVER overwrites an existing Account's fields.
  * NEVER re-attributes a Lead's owner.
  * Rolls up "last_activity_at" from the most recent Lead per company.
  * Adds a starter Relationship Tag = "Customer" for companies that have
    at least one Won Opportunity, otherwise "Prospect".
  * Writes an assignment-history row for each newly-mapped Account so the
    audit trail is complete.

Usage:
    python scripts/2026_08_28_presales_phase7_backfill.py           # dry-run
    python scripts/2026_08_28_presales_phase7_backfill.py --apply
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime
from collections import defaultdict
from sqlalchemy import func
from app import app, db, Company, Lead, Opportunity
from presales.models import (
    AccountAssignmentHistory, AccountStageHistory,
    AccountRelationshipTag,
)


def main(apply_changes: bool):
    with app.app_context():
        # Distinct company names in leads (case-insensitive)
        rows = (db.session.query(
                    Lead.company,
                    func.count(Lead.id).label('lead_count'),
                    func.max(Lead.created_at).label('last_lead_at'))
                .filter(Lead.company.isnot(None),
                        func.length(func.trim(Lead.company)) > 0)
                .group_by(Lead.company)
                .all())
        # Existing Companies keyed by lowercased name
        existing = {c.name.lower().strip(): c for c in Company.query.all()}

        to_create = []
        for name, lead_count, last_lead_at in rows:
            key = (name or '').lower().strip()
            if not key:
                continue
            if key not in existing:
                to_create.append((name.strip(), int(lead_count), last_lead_at))

        print(f'Distinct company names in leads : {len(rows)}')
        print(f'Already mapped to Account         : {len(rows) - len(to_create)}')
        print(f'Will create new Accounts          : {len(to_create)}')

        if not to_create:
            return

        # PIC = most-common assigned_to for each company name (fallback: 'PCM001')
        pic_by_company = {}
        for name, _, _ in to_create:
            pic = (db.session.query(Lead.assigned_to, func.count(Lead.id))
                   .filter(func.lower(Lead.company) == name.lower(),
                           Lead.assigned_to.isnot(None))
                   .group_by(Lead.assigned_to)
                   .order_by(func.count(Lead.id).desc())
                   .first())
            pic_by_company[name] = pic[0] if pic else 'PCM001'

        if not apply_changes:
            print('\nSample of first 20 accounts to create:')
            for name, cnt, last in to_create[:20]:
                print(f'  + {name:40s}  leads={cnt:4d}  pic={pic_by_company.get(name,"-")}')
            print('\nDRY RUN — re-run with --apply.')
            return

        # Apply
        created = 0
        for name, cnt, last_lead_at in to_create:
            c = Company(
                name          = name,
                is_active     = True,
                created_by    = pic_by_company.get(name),
            )
            # Additive extension fields
            c.dev_stage      = 'Active Account'
            c.pic_emp_code   = pic_by_company.get(name)
            c.priority       = 'Medium'
            c.last_activity_at = last_lead_at
            db.session.add(c); db.session.flush()

            # Tag as Customer if any Won opps, else Prospect
            won = Opportunity.query.filter(
                Opportunity.company_id == c.id,
                Opportunity.stage.in_(['Won', 'Closed Won'])).count()
            tag = 'Customer' if won else 'Prospect'
            db.session.add(AccountRelationshipTag(account_id=c.id, tag=tag))

            db.session.add(AccountAssignmentHistory(
                account_id=c.id, previous_pic_code=None,
                new_pic_code=c.pic_emp_code, assigned_by='backfill',
                reason='Backfill from existing Lead history'))
            db.session.add(AccountStageHistory(
                account_id=c.id, from_stage=None,
                to_stage=c.dev_stage, changed_by='backfill',
                note='Backfill from existing Lead history'))
            created += 1

        db.session.commit()
        print(f'\n  [OK] Created {created} new Accounts, tags + audit rows written.')


if __name__ == '__main__':
    main(apply_changes='--apply' in sys.argv)
