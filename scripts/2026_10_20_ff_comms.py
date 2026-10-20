"""
FF-COMMS-01 R2 and R3 — references, templates.

    .venv/bin/python scripts/2026_10_20_ff_comms.py --check
    .venv/bin/python scripts/2026_10_20_ff_comms.py
    .venv/bin/python scripts/2026_10_20_ff_comms.py --down --yes

  ff_crn_sequences     the counter behind <BU>-<C|T><FY>-<NNNNNN>
  ff_email_templates   FF-ENQ, FF-RFQ, FF-QUOTE, FF-REMIND,
                       FF-CONFIRM, FF-BD
  ff_handover_packs    the customer's confirmation, which is the PO
                       on a forwarding job, and the pack built from it

The CRN columns on leads, rfqs and quotes shipped with R1 and are
already in place; this adds the two tables and seeds the templates.

Seeding never overwrites: an administrator's rewrite has to survive
a redeploy, and a seeder that resets the wording every boot is how
somebody's careful edit disappears overnight.

No reference is backfilled. Issuing numbers to eleven thousand
historic leads would consume the series for a year of jobs nobody
is working, and the scheme is for work that starts from here.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect, text                      # noqa: E402

from app import app, db                                   # noqa: E402

TABLES = ('ff_crn_sequences', 'ff_email_templates',
          'ff_handover_packs')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    with app.app_context():
        from app.models.ff import (CrnSequence, FfEmailTemplate,
                                   FfHandoverPack)
        from app.services import ff_templates

        print(f'database: {db.engine.url}')
        have = set(inspect(db.engine).get_table_names())
        for t in TABLES:
            print(f'  {t}: ' + ('present' if t in have else 'absent'))

        if args.check:
            if 'ff_crn_sequences' in have:
                for row in CrnSequence.query.all():
                    print(f'    series {row.bu}-{row.kind}{row.fy} '
                          f'at {row.last_serial}')
            if 'ff_email_templates' in have:
                rows = FfEmailTemplate.query.all()
                print(f'    templates: {len(rows)} '
                      f'({", ".join(sorted(r.code for r in rows)) or "none"})')
                missing = [c for c in ff_templates.CODES
                           if not any(r.code == c for r in rows)]
                if missing:
                    print(f'    to seed: {", ".join(missing)}')
            from app import Lead
            print(f'\n  leads carrying a CRN: '
                  f'{Lead.query.filter(Lead.crn.isnot(None)).count()} '
                  f'(nothing is backfilled)')
            print('\nCHECK ONLY — nothing written.')
            return

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            for model in (FfHandoverPack, FfEmailTemplate,
                          CrnSequence):
                if model.__tablename__ in have:
                    model.__table__.drop(db.engine)
                    print(f'  dropped {model.__tablename__}')
            return

        for model in (CrnSequence, FfEmailTemplate,
                      FfHandoverPack):
            if model.__tablename__ in have:
                print(f'  {model.__tablename__} already present.')
                continue
            model.__table__.create(db.engine)
            print(f'  created {model.__tablename__}')

        made = ff_templates.seed(actor='2026_10_20_ff_comms')
        db.session.commit()
        print(f'  templates seeded: {", ".join(made) or "none needed"}')
        total = FfEmailTemplate.query.count()
        print(f'  templates on file: {total}')


if __name__ == '__main__':
    main()
