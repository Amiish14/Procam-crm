"""
Give #847 "New Lead" leads back to the customers they belong to.

    .venv/bin/python scripts/split_new_lead_account.py --check
    .venv/bin/python scripts/split_new_lead_account.py --apply

Account #847 is a placeholder holding several real customers at
once: Vedanta, Jindal Stainless, Fried-Sped, Luminous and Siemens
all filed under one row called "New Lead". Its open leads are
moved to the right account, creating one only where none exists.

Closed leads are left alone (decided 25 October): moving a lead
that was closed under one account changes the history of a deal
nobody is working, for no benefit.

What it refuses to decide
    `siemens.com`. Five Siemens entities are already on file —
    SIEMENS LIMITED, SIEMENS ENERGY INDIA, SIEMENS GAMESA,
    Siemens-Energy, Siemens-Healthineers — and nothing in the mail
    says which one `srrp.projects.in@siemens.com` belongs to.
    Creating a sixth "Siemens" would make the problem this script
    exists to fix. It is listed for a person instead.

    Six of the ten open leads carry no sender, no name and no
    GSTIN. There is nothing to move them to, and they stay where
    they are — visible in Account check, now that a placeholder
    account counts as no account.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Company, Lead, app, db                    # noqa: E402

PLACEHOLDER_ID = 847
ACTION = 'lead.account_split_from_placeholder'

#: domain -> (existing account id or None, name to create)
#: Written out rather than matched on the fly because each line is
#: a judgement about which legal entity a customer is, and those
#: belong in the open where they can be argued with.
ROUTE = {
    'vedanta.co.in': (988, None),          # the contact is on file there
    'luminousindia.com': (804, None),      # Luminousindia
    'fried-sped.de': (None, 'Fried & Sped GmbH'),
}

#: Domains deliberately not routed, and why.
REFUSED = {
    'siemens.com': ('five Siemens entities are already on file and '
                    'nothing says which one this is — a person '
                    'picks, or a sixth gets created'),
}


def open_leads():
    from app.services import sales_rules
    return (Lead.query
            .filter(Lead.company_id == PLACEHOLDER_ID,
                    Lead.is_archived.isnot(True),
                    Lead.stage.in_(sales_rules.open_stages()))
            .order_by(Lead.id).all())


def domain_of(lead):
    from app.services import account_match, account_suggest
    return account_match.domain_of(account_suggest.sender_of(lead))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--actor', default='DIR12010')
    args = ap.parse_args()
    if args.check == args.apply:
        raise SystemExit('Choose one: --check or --apply.')

    with app.app_context():
        from app.services import account_gate, audit

        print(f'database: {db.engine.url}')
        print(f'MODE:     {"APPLY" if args.apply else "DRY RUN"}\n')

        placeholder = db.session.get(Company, PLACEHOLDER_ID)
        if placeholder is None:
            raise SystemExit(f'No account #{PLACEHOLDER_ID}.')
        leads = open_leads()
        print(f'  #{PLACEHOLDER_ID} "{placeholder.name}" — '
              f'{len(leads)} open lead(s)\n')

        moved, refused, nothing = [], [], []
        for lead in leads:
            domain = domain_of(lead)
            if not domain:
                nothing.append(lead)
            elif domain in REFUSED:
                refused.append((lead, domain, REFUSED[domain]))
            elif domain in ROUTE:
                moved.append((lead, domain, ROUTE[domain]))
            else:
                nothing.append(lead)

        for lead, domain, (account_id, create) in moved:
            target = (db.session.get(Company, account_id) if account_id
                      else None)
            label = (target.name if target is not None
                     else f'{create} (to create)')
            print(f'    #{lead.id:<7}{lead.stage:<16}{domain:<22}'
                  f'-> {label}')
        for lead, domain, why in refused:
            print(f'    #{lead.id:<7}{lead.stage:<16}{domain:<22}'
                  f'NOT MOVED — {why}')
        for lead in nothing:
            print(f'    #{lead.id:<7}{lead.stage:<16}'
                  f'{"(no sender)":<22}nothing to identify it; stays '
                  f'put, and now shows in Account check')

        if args.check:
            print(f'\nDRY RUN — nothing written. {len(moved)} would '
                  f'move, {len(refused)} refused, {len(nothing)} have '
                  f'nothing to go on.')
            return

        done = []
        for lead, domain, (account_id, create) in moved:
            target = (db.session.get(Company, account_id) if account_id
                      else None)
            if target is None:
                target = Company(name=create, is_active=True,
                                 email_domains=[domain],
                                 created_by=args.actor)
                db.session.add(target)
                db.session.flush()
                audit.record('account.created', 'company', target.id,
                             new={'name': create, 'domain': domain},
                             actor=args.actor,
                             reason=f'split out of placeholder '
                                    f'#{PLACEHOLDER_ID}')
            elif domain not in [str(d).lower() for d in
                                (target.email_domains or [])]:
                was = list(target.email_domains or [])
                target.email_domains = was + [domain]
                audit.record('account.domain_added', 'company',
                             target.id, old={'email_domains': was},
                             new={'email_domains': target.email_domains},
                             actor=args.actor,
                             reason=f'{domain} was filed under '
                                    f'placeholder #{PLACEHOLDER_ID}')

            vertical = account_gate.vertical_of(lead)
            owner = account_gate.owner_for(target, vertical)
            lead.company_id = target.id
            audit.record(ACTION, 'lead', lead.id,
                         old={'company_id': PLACEHOLDER_ID},
                         new={'company_id': target.id,
                              'account': target.name},
                         actor=args.actor,
                         reason=f'moved off the "{placeholder.name}" '
                                f'placeholder; the sender is at '
                                f'{domain}')
            done.append((lead, target, owner))

        db.session.commit()
        print(f'\n  {len(done)} lead(s) moved:')
        for lead, target, owner in done:
            note = ('' if owner else
                    '  (no owner for its vertical yet — it will '
                    'show in Account check)')
            print(f'    #{lead.id} -> #{target.id} {target.name}{note}')
        print(f'  {len(refused)} refused, {len(nothing)} left with '
              f'nothing to identify them. Closed leads untouched.')


if __name__ == '__main__':
    main()
