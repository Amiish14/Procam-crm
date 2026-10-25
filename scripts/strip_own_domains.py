"""
No account may claim a domain of ours.

    .venv/bin/python scripts/strip_own_domains.py --check
    .venv/bin/python scripts/strip_own_domains.py --apply
    .venv/bin/python scripts/strip_own_domains.py --down --yes

Account #983 "Unknown" listed `procamgroup.in` among its email
domains. That is an exact matching rule, so with
FEATURE_ACCOUNT_FIRST on, every internal forward would have
matched it and been filed under "Unknown". The matcher now refuses
such an account outright, but the data is still wrong and the next
rule written against `email_domains` would hit it again.

Only our own domains are removed. The account itself, its leads
and its other domains are untouched — what to do with a row
holding 4,435 leads is a separate decision.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Company, app, db                          # noqa: E402

ACTION = 'account.own_domain_removed'


def ours(domain):
    from app.services.mail_signals import OUR_DOMAINS
    domain = (domain or '').strip().lower()
    return any(domain == d or domain.endswith('.' + d)
               for d in OUR_DOMAINS)


def plan():
    out = []
    for account in Company.query.all():
        domains = [str(d).strip() for d in
                   (account.email_domains or []) if str(d).strip()]
        bad = [d for d in domains if ours(d)]
        if bad:
            out.append((account, bad,
                        [d for d in domains if not ours(d)]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()
    if len([a for a in (args.check, args.apply, args.down) if a]) != 1:
        raise SystemExit('Choose one: --check, --apply or --down.')

    with app.app_context():
        from app.services import audit

        print(f'database: {db.engine.url}')
        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            from app.models.audit import AuditEvent
            back = 0
            for ev in AuditEvent.query.filter_by(action=ACTION).all():
                account = db.session.get(Company, int(ev.entity_id))
                was = (ev.old_value or {}).get('email_domains')
                if account is None or was is None:
                    continue
                account.email_domains = was
                back += 1
            db.session.commit()
            print(f'  {back} account(s) restored')
            return

        rows = plan()
        print(f'MODE:     {"APPLY" if args.apply else "DRY RUN"}\n')
        if not rows:
            print('  no account claims one of our domains')
            return
        for account, bad, keep in rows:
            leads = db.session.execute(db.text(
                'SELECT COUNT(*) FROM leads WHERE company_id = :i'),
                {'i': account.id}).scalar()
            print(f'  #{account.id} "{account.name}" — {leads} lead(s)')
            print(f'      remove: {", ".join(bad)}')
            print(f'      keep:   {", ".join(keep) or "(none left)"}')

        if args.check:
            print('\nDRY RUN — nothing written.')
            return

        for account, bad, keep in rows:
            was = list(account.email_domains or [])
            account.email_domains = keep
            audit.record(ACTION, 'company', account.id,
                         old={'email_domains': was},
                         new={'email_domains': keep},
                         actor='CRM-ACCOUNT-FIRST-01',
                         reason=f'{", ".join(bad)} is ours; an '
                                f'account holding it matches every '
                                f'internal forward on an exact rule')
        db.session.commit()
        print(f'\n  {len(rows)} account(s) corrected, an audit row '
              f'each. Leads and other domains untouched.')


if __name__ == '__main__':
    main()
