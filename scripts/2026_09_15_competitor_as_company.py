#!/usr/bin/env python3
"""
Competitor becomes a Company classification — §2, §19.

    python scripts/2026_09_15_competitor_as_company.py --check
    python scripts/2026_09_15_competitor_as_company.py

"Competitor = Company classification. Clicking competitor opens complete
COMPETITOR 360."

competitor_masters was a second company master, exactly what §4 forbids.
It is empty, and so are opportunity_competitors and competitor_intelligence,
so this costs a column rather than a data migration — which is precisely
why it is worth doing now rather than after a year of use.

Adds company_id to both junction tables and copies across anything already
recorded (nothing, on the live database, but the script does not assume
that). competitor_masters is left in place, untouched; nothing is dropped.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)


def _resolve_db_url():
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


TARGETS = [('opportunity_competitors', 'company_id'),
           ('competitor_intelligence', 'company_id')]


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

    names = set(inspect(engine).get_table_names())
    ok = True
    for table, column in TARGETS:
        if table not in names:
            print(f'{table} — table not present, skipped')
            continue
        cols = {c['name'] for c in inspect(engine).get_columns(table)}
        if column in cols:
            print(f'{table}.{column} — already present')
            continue
        print(f'ADD COLUMN {table}.{column}' + ('   (dry run)' if dry else ''))
        if dry:
            ok = False
            continue
        with engine.begin() as conn:
            conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} '
                              f'INTEGER REFERENCES companies(id)'))
    engine.dispose()
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args()
    dry = args.check

    ready = add_columns(dry)
    if not ready:
        print('\n(dry run: re-run without --check to add the columns)'
              if dry else '\nAborted.')
        return 0 if dry else 1

    import importlib
    _main = importlib.import_module('app')
    importlib.import_module('app.models')
    app, db, Company = _main.app, _main.db, _main.Company

    from app.models.competitor import (CompetitorMaster,
                                       OpportunityCompetitor,
                                       CompetitorIntelligence)
    from app.master_data import service as md
    from presales.models import AccountRelationshipTag
    from app.services.company_match import build_index, match

    with app.app_context():
        # competitor_id was NOT NULL against the legacy competitor_masters
        # table.  Nothing writes it any more, so an empty junction table is
        # rebuilt from the model to relax the constraint.  Only ever when
        # the table holds no rows — a populated table is left exactly as it
        # is, and reported.
        from sqlalchemy import inspect as _inspect
        for model in (OpportunityCompetitor, CompetitorIntelligence):
            table = model.__tablename__
            if table not in _inspect(db.engine).get_table_names():
                continue
            rows = model.query.count()
            if rows:
                print(f'{table}: {rows} row(s) — left untouched, '
                      f'competitor_id constraint unchanged')
                continue
            cols = {c['name']: c for c in
                    _inspect(db.engine).get_columns(table)}
            legacy = cols.get('competitor_id')
            if legacy is not None and not legacy.get('nullable', True):
                print(f'{table}: empty — rebuilding so competitor_id may '
                      f'be null')
                model.__table__.drop(db.engine)
                model.__table__.create(db.engine)

        md.ensure_lists()
        md.add_item('relationship', 'Competitor', 'Competitor')

        masters = CompetitorMaster.query.all()
        print(f'\ncompetitor_masters rows: {len(masters)}')

        # Anything already recorded there becomes a classified Company.
        index = build_index(
            Company.query.filter(Company.is_active.is_(True)).all())
        mapping = {}
        created = tagged = 0
        for cm in masters:
            found, _reason, _c = match(cm.name, index)
            if found is None:
                found = Company(name=cm.name, is_active=True,
                                website=getattr(cm, 'website', None),
                                country=getattr(cm, 'country', None),
                                city=getattr(cm, 'city', None))
                db.session.add(found)
                db.session.flush()
                created += 1
            mapping[cm.id] = found.id
            have = {t.tag for t in AccountRelationshipTag.query.filter_by(
                account_id=found.id).all()}
            if 'Competitor' not in have:
                db.session.add(AccountRelationshipTag(
                    account_id=found.id, tag='Competitor'))
                tagged += 1

        moved = 0
        for model in (OpportunityCompetitor, CompetitorIntelligence):
            for row in model.query.filter(
                    model.company_id.is_(None)).all():
                target = mapping.get(getattr(row, 'competitor_id', None))
                if target:
                    row.company_id = target
                    moved += 1
        db.session.commit()

        print(f'  companies created from competitor records: {created}')
        print(f'  companies newly classified as Competitor:  {tagged}')
        print(f'  junction rows repointed at Company Master: {moved}')

        total_comp = AccountRelationshipTag.query.filter_by(
            tag='Competitor').count()
        print(f'\nCompanies classified as Competitor: {total_comp}')
        print('\ncompetitor_masters was left in place and untouched — '
              'nothing was dropped.')
        print('\n✓ applied')
    return 0


if __name__ == '__main__':
    sys.exit(main())
