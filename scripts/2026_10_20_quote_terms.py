"""
CRM 2.0 R5 — the standard terms, and the ones added to one quote.

    .venv/bin/python scripts/2026_10_20_quote_terms.py --check
    .venv/bin/python scripts/2026_10_20_quote_terms.py
    .venv/bin/python scripts/2026_10_20_quote_terms.py --seed
    .venv/bin/python scripts/2026_10_20_quote_terms.py --down --yes

  quote_term_sets            standard terms per vertical, versioned
  quote_special_conditions   conditions on one quote

Additive. `--seed` writes a first version of the standard terms for
each vertical, clearly marked as a draft for the commercial team to
replace — the wording below is a placeholder that says what a term
is for, not Procam's actual contract language, and a quote must not
go out carrying it unreviewed.
"""
import argparse
import os
import sys
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect                            # noqa: E402

from app import app, db                                   # noqa: E402

TABLES = ('quote_term_sets', 'quote_special_conditions')

#: Placeholders. Each says what the clause is for so that the person
#: replacing it knows what is missing; none of it is legal wording.
DRAFT = {
    'Freight Forwarding':
        'DRAFT — replace before issuing.\n'
        '1. Rates are valid for the period stated and are subject to '
        'carrier surcharges ruling at the time of shipment.\n'
        '2. Rates exclude duties, taxes and statutory levies unless '
        'a line on this quote says otherwise.\n'
        '3. Liability is limited as per the governing convention for '
        'the mode carried.\n'
        '4. Demurrage, detention and storage beyond the free period '
        'are to the client\'s account.\n'
        '5. Standard trading conditions apply.',
    'Project Freight':
        'DRAFT — replace before issuing.\n'
        '1. Rates assume the route survey findings stated and are '
        'subject to revision if the route, permits or escort '
        'requirements change.\n'
        '2. Waiting beyond the free period at either end is '
        'chargeable.\n'
        '3. Client to provide a level, compacted and accessible '
        'working area.\n'
        '4. Statutory permits are arranged at cost unless stated.',
    'General Transport':
        'DRAFT — replace before issuing.\n'
        '1. Rates are per trip for the vehicle stated and assume '
        'loading and unloading within the free time given.\n'
        '2. Detention is chargeable beyond the free period.\n'
        '3. Toll, octroi, entry tax and permits are at actuals '
        'unless included in a line.\n'
        '4. Transit times are estimates, not guarantees.',
    'Heavy Transport':
        'DRAFT — replace before issuing.\n'
        '1. Rates assume the route survey and dimensions stated.\n'
        '2. Road strengthening, tree cutting, line shifting and '
        'police escort are at actuals unless quoted.\n'
        '3. Client to arrange cranes at both ends unless a line '
        'provides them.\n'
        '4. Idle time caused by site or permit delays is '
        'chargeable.',
    'Installation':
        'DRAFT — replace before issuing.\n'
        '1. Rates assume continuous access to the work front for '
        'the duration stated.\n'
        '2. Power, water, lighting and a safe working area are to '
        'the client\'s account.\n'
        '3. Standby caused by site readiness is chargeable.\n'
        '4. Work is carried out in daylight hours unless agreed.',
    'Warehousing':
        'DRAFT — replace before issuing.\n'
        '1. Storage is charged per the unit and period stated, part '
        'periods counting as whole.\n'
        '2. Inbound and outbound handling are charged separately.\n'
        '3. Goods are held subject to a general lien for unpaid '
        'charges.\n'
        '4. Insurance of stored goods is the client\'s '
        'responsibility unless a line provides it.',
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--seed', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    with app.app_context():
        from app.models.quote_terms import (QuoteSpecialCondition,
                                            QuoteTermSet)
        print(f'database: {db.engine.url}')
        have = set(inspect(db.engine).get_table_names())
        for t in TABLES:
            print(f'  {t}: ' + ('present' if t in have else 'absent'))

        if args.check:
            if 'quote_term_sets' in have:
                print(f'\n  term sets:          '
                      f'{QuoteTermSet.query.count()}')
                print(f'  current per vertical:')
                for row in QuoteTermSet.query.filter_by(
                        is_current=True).order_by(
                            QuoteTermSet.vertical.asc()).all():
                    print(f'    {row.vertical:<22} v{row.version}  '
                          f'{row.title}')
            if 'quote_special_conditions' in have:
                print(f'  special conditions: '
                      f'{QuoteSpecialCondition.query.count()}')
            print('\nCHECK ONLY — nothing written.')
            return

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            for model in (QuoteSpecialCondition, QuoteTermSet):
                if model.__tablename__ in have:
                    model.__table__.drop(db.engine)
                    print(f'  dropped {model.__tablename__}')
            return

        for model in (QuoteTermSet, QuoteSpecialCondition):
            if model.__tablename__ in have:
                print(f'  {model.__tablename__} already present.')
                continue
            model.__table__.create(db.engine)
            print(f'  created {model.__tablename__}')

        if not args.seed:
            print('\nNo terms written. `--seed` adds a draft first '
                  'version per vertical for the commercial team to '
                  'replace.')
            return

        from app.services import quote_terms
        made = []
        for vertical, body in sorted(DRAFT.items()):
            if quote_terms.current_for(vertical) is not None:
                print(f'  {vertical}: already has a current version.')
                continue
            row = quote_terms.publish(
                vertical, body, title='Standard Terms and Conditions '
                                      '(DRAFT)',
                actor='CRM2-R5', effective_from=date.today())
            made.append(row)
        db.session.commit()
        print(f'\n  {len(made)} draft term set(s) written. Every one '
              f'says DRAFT in its first line: replace the wording '
              f'before a quote goes out.')


if __name__ == '__main__':
    main()
