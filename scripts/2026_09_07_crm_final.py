"""
CRM Final Drop — Phases 12-15 migration.

Adds tables (idempotent, additive):
    business_card_imports    — Phase 12 (business card scan)
    help_articles            — Phase 13 (self-help)
    help_tooltips            — Phase 13 (self-help)

Seeds ~16 skeleton HelpArticle rows — one per top-level section listed
in the CRM upgrade programme.  Admins can then edit the bodies via
`/admin/help`.  Reruns are safe — seeded slugs are skipped when they
already exist.

Usage:
    python scripts/2026_09_07_crm_final.py             # apply
    python scripts/2026_09_07_crm_final.py --check     # dry-run
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import app as flask_app, db   # noqa: E402


# ─── Seed help articles ─────────────────────────────────────────────────
# 16 skeleton articles, one per section handled by the CRM.
SEED_HELP_ARTICLES = [
    # (slug, section, title, what_it_is)
    ('my-work', 'my_work', 'My Work',
     'Your role-aware landing page. Shows the tasks assigned to you '
     'today, grouped by priority.'),
    ('dashboard', 'dashboard', 'Dashboard',
     'Company-wide pipeline, funnel and outcome KPIs. Filters by '
     'vertical, PIC and date range.'),
    ('accounts', 'accounts', 'Accounts',
     'The Company / Customer master. Every Lead, RFQ and Opportunity '
     'references an Account, so keep them tidy.'),
    ('contacts', 'contacts', 'Contacts',
     'People at each Account — decision makers, influencers, procurement.'),
    ('projects', 'projects', 'Projects',
     'Long-running customer engagements. Fed by Won Opportunities and '
     'monitored on a review cadence.'),
    ('leads', 'leads', 'Leads',
     'Inbound enquiries — email, walk-in, referral, form. Convert each '
     'lead into an Opportunity when qualified.'),
    ('rfqs', 'rfqs', 'RFQs',
     'Request for Quotation. Created from a Lead or an existing Account. '
     'Drives Rate Sourcing and Quote preparation.'),
    ('rate-sourcing', 'rate_sourcing', 'Rate Sourcing',
     'Ops team collects vendor rates for each RFQ line item. Feeds the '
     'Quote engine.'),
    ('quotes', 'quotes', 'Quotes',
     'The priced proposal you send to the customer. Draft → Submit → '
     'Negotiation → Won/Lost.'),
    ('won-lost', 'won_lost', 'Won / Lost',
     'Terminal outcomes. Won triggers TMS handover; Lost triggers a '
     'competitor-log task.'),
    ('competitors', 'competitors', 'Competitors',
     'Competitor Master — canonical competitors, their contacts, and '
     'the intelligence timeline.'),
    ('overseas', 'overseas', 'Overseas Partners',
     'International partner agents who feed Procam leads or execute '
     'destination legs.'),
    ('reports', 'reports', 'Reports',
     'Cross-cutting reports for Action Management, Competitors and '
     'Account Development. Every report exports to Excel.'),
    ('kpi', 'kpi', 'KPIs',
     'Per-user and per-team KPIs — SLA hit rate, throughput, '
     'contribution to funnel and won value.'),
    ('tms-handover', 'tms_handover', 'TMS Handover',
     'When an Opportunity is Won, a structured payload is handed to the '
     'Transport Management System (TMS) for execution.'),
    ('business-cards', 'accounts', 'Business Card Scan',
     'Snap a business card and let Claude Vision extract Name, '
     'Designation, Company, Email, Phone and address into a review form.'),
]


def _seed_articles(dry=False):
    from app.models.help_content import HelpArticle
    inserted = 0
    for i, (slug, section, title, what) in enumerate(SEED_HELP_ARTICLES):
        if HelpArticle.query.filter_by(slug=slug).first():
            continue
        if dry:
            inserted += 1
            continue
        db.session.add(HelpArticle(
            slug=slug, section=section, title=title,
            what_it_is=what,
            when_to_use='(admin to edit)',
            how_to_use='(admin to edit)',
            required_fields='(admin to edit)',
            what_happens_next='(admin to edit)',
            common_mistakes='(admin to edit)',
            display_order=i,
            is_active=True,
            role_visibility=[],
        ))
        inserted += 1
    if not dry:
        db.session.commit()
    return inserted


def _count_new_tables():
    counts = {}
    for tbl in ('business_card_imports', 'help_articles', 'help_tooltips'):
        try:
            n = db.session.execute(
                db.text(f'SELECT COUNT(*) FROM {tbl}')).scalar()
            counts[tbl] = int(n or 0)
        except Exception:
            counts[tbl] = None
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true',
                    help='dry-run — do not write')
    args = ap.parse_args()

    # Force new models to import so their tables register on metadata.
    import app.models.business_card    # noqa: F401
    import app.models.help_content     # noqa: F401

    with flask_app.app_context():
        if args.check:
            print('== DRY-RUN — no writes will be performed ==')

            new_articles = 0
            try:
                from app.models.help_content import HelpArticle
                for row in SEED_HELP_ARTICLES:
                    if not HelpArticle.query.filter_by(slug=row[0]).first():
                        new_articles += 1
            except Exception as e:
                print(f'  [warn] help_articles introspection failed: {e}')
                new_articles = len(SEED_HELP_ARTICLES)

            print('  WOULD create tables via db.create_all() '
                  '(existing tables untouched)')
            print(f'  WOULD seed NEW help_articles:  {new_articles}')

            counts = _count_new_tables()
            print('  Current row counts (None = table missing):')
            for k, v in counts.items():
                print(f'    {k}: {v}')
            print('== end dry-run ==')
            return

        db.create_all()
        new_articles = _seed_articles()
        counts = _count_new_tables()

        print('== 2026_09_07_crm_final.py — summary ==')
        print('  tables created / verified via db.create_all()')
        print(f'  new help_articles seeded:  {new_articles}')
        print('  current row counts:')
        for k, v in counts.items():
            print(f'    {k}: {v}')
        print('  done.')


if __name__ == '__main__':
    main()
