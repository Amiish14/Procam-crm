#!/usr/bin/env python3
"""
Link Leads and Contacts to Company Master — §7, §65.

    python scripts/2026_09_12_link_companies.py --check
    python scripts/2026_09_12_link_companies.py

Leads and Contacts reference companies by NAME, as text, with no foreign
key.  That is why 4,269 opportunities are invisible to account reports and
why Company 360 cannot be built: there is nothing to join on.

The audit measured 94.8% of lead company names matching a Company record
exactly, so most of this is automatic.  The rest is the point of §65:

    exactly one match   → link it
    several matches     → review queue, with the candidates
    no match            → review queue, with near neighbours as suggestions

Nothing is guessed.  The text column is left in place, so this is additive
and reversible: clearing company_id restores the previous state exactly.

Run scripts/2026_09_11_company_dedup.py FIRST.  Linking before merging
attaches leads to duplicates that are about to be retired.
"""
import argparse
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)


def _resolve_db_url():
    """Computed the way app.py does, before app.py is imported — it runs
    init_db() at import, which would fail on the missing column."""
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_ROOT, '.env'))
    except Exception:
        pass
    url = os.environ.get('DATABASE_URL', 'sqlite:///procam_crm.db')
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql://', 1)
    if url.startswith('sqlite:///') and not url.startswith('sqlite:////'):
        rel = url[len('sqlite:///'):]
        if not os.path.isabs(rel):
            cand = os.path.join(_ROOT, 'instance', rel)
            if not os.path.exists(cand):
                alt = os.path.join(_ROOT, rel)
                cand = alt if os.path.exists(alt) else cand
            url = 'sqlite:///' + cand
    return url


def add_columns(dry):
    from sqlalchemy import create_engine, inspect, text

    url = _resolve_db_url()
    if url.startswith('sqlite:///'):
        print(f'database: {url[len("sqlite:///"):]}')
    engine = create_engine(url)

    with engine.connect() as conn:
        n = conn.execute(text('SELECT COUNT(*) FROM employees')).scalar()
    print(f'employees in this database: {n}')
    if not n:
        print('!! no employees here — this is not the live database.')
        engine.dispose()
        return False

    ok = True
    for table in ('leads', 'contacts'):
        cols = {c['name'] for c in inspect(engine).get_columns(table)}
        if 'company_id' in cols:
            print(f'{table}.company_id — already present')
            continue
        print(f'ADD COLUMN {table}.company_id' + ('   (dry run)' if dry else ''))
        if dry:
            ok = False
            continue
        with engine.begin() as conn:
            conn.execute(text(f'ALTER TABLE {table} '
                              f'ADD COLUMN company_id INTEGER '
                              f'REFERENCES companies(id)'))
        print('  ✓ added')
    engine.dispose()
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()
    dry = args.check

    ready = add_columns(dry)
    if not ready:
        print('\n(dry run: re-run without --check to add the columns, then '
              'linking can be previewed)' if dry else '\nAborted.')
        return 0 if dry else 1

    import importlib
    _main = importlib.import_module('app')
    importlib.import_module('app.models')
    app, db = _main.app, _main.db
    Lead, Contact, Company = _main.Lead, _main.Contact, _main.Company

    from app.services.company_match import (build_index, match,
                                            NO_MATCH, AMBIGUOUS)
    from app.models.data_mapping import DataMappingQueue, MappingStatus

    with app.app_context():
        insp = db.inspect(db.engine)
        if 'data_mapping_queue' not in insp.get_table_names():
            print('CREATE TABLE data_mapping_queue')
            DataMappingQueue.__table__.create(db.engine)

        # Only companies still standing after de-duplication.
        index = build_index(
            Company.query.filter(Company.is_active.is_(True)).all())
        print(f'\nCompany Master: {sum(len(v) for v in index.values())} '
              f'active records, {len(index)} distinct names')

        already = {(q.entity_type, q.entity_id, q.field)
                   for q in DataMappingQueue.query.filter(
                       DataMappingQueue.status.in_(
                           MappingStatus.OPEN)).all()}

        for model, label in ((Lead, 'Lead'), (Contact, 'Contact')):
            rows = model.query.filter(model.company_id.is_(None))
            if args.limit:
                rows = rows.limit(args.limit)
            rows = rows.all()

            stats = Counter()
            queued = 0
            for row in rows:
                company, reason, candidates = match(row.company, index)
                if company is not None:
                    stats['linked'] += 1
                    row.company_id = company.id
                    continue

                stats[reason] += 1
                key = (label, row.id, 'company')
                if key in already:
                    continue
                queued += 1
                db.session.add(DataMappingQueue(
                    entity_type=label, entity_id=row.id, field='company',
                    raw_value=(row.company or '')[:300],
                    reason=reason, candidates=candidates,
                    status=MappingStatus.PENDING))

            total = len(rows)
            pct = (stats['linked'] / total * 100) if total else 0
            print(f'\n{label}s needing a link: {total}')
            print(f'  linked automatically   {stats["linked"]:>7}'
                  f'  ({pct:.1f}%)')
            print(f'  no match → review      {stats[NO_MATCH]:>7}')
            print(f'  ambiguous → review     {stats[AMBIGUOUS]:>7}')
            print(f'  queued for a decision  {queued:>7}')

        db.session.commit()

        pending = DataMappingQueue.query.filter_by(
            status=MappingStatus.PENDING).count()
        print(f'\nReview queue: {pending} record(s) awaiting a decision.')
        print('Nothing was guessed — every uncertain name is queued.')

        linked_leads = Lead.query.filter(Lead.company_id.isnot(None)).count()
        total_leads = Lead.query.count()
        print(f'\nLeads now linked to Company Master: '
              f'{linked_leads}/{total_leads}')
        print('\n✓ applied.  The `company` text column is unchanged, so '
              'clearing company_id restores the previous state exactly.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
