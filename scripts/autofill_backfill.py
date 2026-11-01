"""
Write onto the lead what its mail already said.

    .venv/bin/python scripts/autofill_backfill.py --check
    .venv/bin/python scripts/autofill_backfill.py --apply
    .venv/bin/python scripts/undo_batch.py --batch cockpit02.autofill.<date> --apply

CRM-COCKPIT-02 §5.1 and §5.2. `email_extracted` has held origin and
destination for about 400 leads, cargo type for 420, weight for 197
and dimensions for 122 — and the lead's own `origin`, `destination`,
`industry`, `state` and `city` were empty on all 915 open leads. Every
one of those is a field somebody was being asked to type while the
answer sat in the same record, in a JSON blob no report can query.

Two sources, and the order is deliberate: the account first (industry,
state and city describe the customer), then the mail (the cargo and
the route describe this enquiry). Where both have something, the
account wins — it is the record of the customer, and a mail's
signature block is one enquiry's idea of it.

Nothing is overwritten. A field a person has filled is left exactly as
it is; this only ever writes into a blank. Everything it writes is
marked "auto — confirm" in `auto_filled_json`, and stays marked until
somebody agrees with it: a figure the CRM read and a figure its owner
typed are not the same thing.
"""
import argparse
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402

BATCH = 'cockpit02.autofill'
NOTE = ('01-Nov-2026 CRM-COCKPIT-02: lead fields filled from the '
        'enquiry mail and the account (rule: §5.1 / §5.2, blanks '
        'only, marked auto — confirm)')


def population(open_only=True):
    from app.services import sales_rules as rules

    q = Lead.query.filter(Lead.is_archived.isnot(True))
    if open_only:
        q = q.filter(Lead.stage.in_(rules.open_stages()))
    return q.order_by(Lead.id).all()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--all-leads', action='store_true',
                    help='closed leads too; by default open only')
    ap.add_argument('--actor', default='CRM-COCKPIT-02')
    args = ap.parse_args()
    if not (args.check or args.apply):
        args.check = True

    with app.app_context():
        from app.services import autofill as af, bulk_log

        print(f'database: {db.engine.url}')
        print('MODE:     ' + ('APPLY' if args.apply
                              else 'CHECK — nothing written') + '\n')

        leads = population(open_only=not args.all_leads)
        print(f'  leads examined                 {len(leads)}')

        per_field, touched = Counter(), 0
        samples = []
        for lead in leads:
            filled = af.apply(lead, dry=True)
            if not filled:
                continue
            touched += 1
            for field in filled:
                per_field[field] += 1
            if len(samples) < 10:
                samples.append((lead.id, lead.company or '', filled))
        print(f'  leads with something to fill   {touched}\n')
        for field, n in per_field.most_common():
            print(f'    {field:<24} {n}')

        if samples:
            print('\n  a sample of what would be written:')
            for lead_id, company, filled in samples:
                shown = ', '.join(f'{k}={str(v)[:18]}'
                                  for k, v in list(filled.items())[:3])
                print(f'    #{lead_id:<7} {company[:24]:<24} {shown}')

        gaps = af.accounts_missing_details(limit=None)
        print(f'\n  accounts with no industry, state or city: '
              f'{len(gaps)}')
        if gaps:
            print('    these are the "Account details missing" list; '
                  'the CRM can\n    offer a value for some of them:')
            from app import Company
            offered = 0
            for row in gaps[:200]:
                account = db.session.get(Company, row['id'])
                if account is not None and af.guess_account_details(account):
                    offered += 1
            print(f'    of the first {min(len(gaps), 200)}, a value '
                  f'can be offered for {offered}')

        if args.check:
            print('\nCHECK ONLY — nothing written. --apply writes '
                  'these;\nscripts/undo_batch.py puts them back.')
            return 0

        # The real pass. Every field goes through the batch, so the
        # whole run is one `undo_batch.py` away from being undone.
        from app.services import audit

        with bulk_log.batch(BATCH, note=NOTE, actor=args.actor) as log:
            for lead in leads:
                filled = af.apply(lead, log=log)
                if not filled:
                    continue
                audit.record('lead.autofilled', 'lead', lead.id,
                             new={'filled': sorted(filled)},
                             actor=args.actor, reason=NOTE,
                             commit=False)
            batch_name, written = log.name, len(log)

        try:
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'\n  !! ROLLED BACK — nothing was written: '
                  f'{type(exc).__name__}: {str(exc)[:200]}')
            return 1

        print(f'\n  {written} field(s) written, every one marked '
              f'"auto — confirm".')
        print(f'\n  reversible log batch: {batch_name}')
        print(f'  undo: .venv/bin/python scripts/undo_batch.py '
              f'--batch {batch_name} --apply')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
