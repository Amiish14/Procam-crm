#!/usr/bin/env python3
"""
Master Data Center — §59-62.

    python scripts/2026_09_10_master_data.py --check
    python scripts/2026_09_10_master_data.py

Creates master_lists / master_items and seeds every vocabulary from the
values ALREADY IN THE DATABASE, plus the lists the code currently
hard-codes.  Seeding from live data matters: verticals only exist as free
text on employees and leads, so the real vocabulary is whatever people
have typed — inventing a tidy list would orphan existing records.

Idempotent.
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

from app.models.master_data import MasterList, MasterItem      # noqa: E402
from app.master_data import service as md                      # noqa: E402


def distinct(model, column, limit=400):
    """Live values for a column, most common first."""
    try:
        rows = db.session.query(getattr(model, column)).all()
    except Exception:
        return []
    counts = Counter((r[0] or '').strip() for r in rows)
    counts.pop('', None)
    return [v for v, _n in counts.most_common(limit)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args()
    dry = args.check

    with app.app_context():
        insp = db.inspect(db.engine)
        for table, model in (('master_lists', MasterList),
                             ('master_items', MasterItem)):
            if table not in insp.get_table_names():
                print(f'CREATE TABLE {table}' + ('   (dry run)' if dry else ''))
                if not dry:
                    model.__table__.create(db.engine)
            else:
                print(f'{table} — already present')

        if dry and 'master_lists' not in db.inspect(db.engine).get_table_names():
            print('\n(dry run: cannot preview seeding until the tables exist)')
            return 0

        if not dry:
            n = md.ensure_lists()
            print(f'\nList registrations created: {n}')

        from app import Employee, Lead, Company, Contact
        from presales.models import AccountRelationshipTag, ACCOUNT_DEV_STAGES

        # Everything §5 asks for, unioned with whatever is already tagged.
        relationship_seed = [
            'Customer', 'Prospective Customer', 'Account', 'Strategic Account',
            'Overseas Agent', 'Overseas Partner', 'Competitor', 'Vendor',
            'Supplier', 'EPC', 'Project Owner', 'PMC', 'Consultant',
            'Manufacturer', 'Technology Provider', 'Logistics Partner',
            'Network Member', 'RFQ Source', 'Other',
        ]

        plan = {
            'vertical': sorted(set(distinct(Employee, 'vertical'))
                               | set(distinct(Lead, 'procam_vertical'))),
            'industry': distinct(Company, 'industry')
                        or distinct(Lead, 'industry'),
            'relationship': sorted(
                set(relationship_seed)
                | set(distinct(AccountRelationshipTag, 'tag'))),
            'network': ['PCN', 'THLG', 'WCA', 'Other'],
            'service': ['Heavy Transport', 'Project Freight', 'Warehousing',
                        'Installation', 'Customs Clearance', 'Chartering',
                        'Other'],
            'lost_reason': list(getattr(_main, 'LOSS_REASONS', []))
                           or distinct(Lead, 'lost_reason'),
            'intel_type': [
                'Competitive Encounter', 'Project Won', 'Customer Won',
                'Tender Result', 'Known Quote', 'Pricing Intelligence',
                'New Equipment', 'New Office', 'New Geography', 'Partnership',
                'Acquisition', 'Senior Appointment', 'Salesperson Appointment',
                'Public News', 'LinkedIn', 'Internal Intelligence',
            ],
            'assignment_role': ['Lead Driver', 'Rate Sourcing',
                                'Account Owner', 'Project PIC',
                                'Technical Support', 'Vertical Head',
                                'Global Account Manager',
                                'Management Sponsor'],
            'task_type': ['Call', 'Email', 'Meeting', 'Visit', 'Review',
                          'Approval', 'Follow-up', 'Handover', 'Other'],
            'priority': ['Critical', 'High', 'Medium', 'Low'],
            'source': distinct(Lead, 'source') or ['email', 'manual'],
            'project_stage': [],
            'account_stage': list(ACCOUNT_DEV_STAGES),
        }
        try:
            from presales.models_projects import PROJECT_STAGES
            plan['project_stage'] = list(PROJECT_STAGES)
        except Exception:
            pass

        print('\nSeeding from live data + code constants')
        total_new = 0
        for key, vals in plan.items():
            vals = [v for v in vals if v and str(v).strip()]
            if not vals:
                print(f'  {key:18} — nothing found')
                continue
            have = {i.code for i in MasterItem.query.filter_by(
                list_key=key).all()} if not dry else set()
            new = [v for v in vals if v not in have]
            print(f'  {key:18} {len(vals):>4} value(s), {len(new):>4} new')
            if not dry:
                for order, v in enumerate(vals):
                    md.add_item(key, code=v, label=v,
                                sort_order=(order + 1) * 10,
                                actor='migration')
            total_new += len(new)

        print(f'\n{"Would add" if dry else "Added"}: {total_new} values')

        if not dry:
            print('\nVerticals now configurable:')
            for i in md.items('vertical'):
                print(f'    {i.code}')

        print('\n' + ('Dry run — nothing written.' if dry else '✓ applied'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
