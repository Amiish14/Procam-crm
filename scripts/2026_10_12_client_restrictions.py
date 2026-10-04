"""
The client block and caution register — schema and first entry.

Adds three tables and one Master Data list:

    client_restrictions              one row per decision
    client_restriction_events        everything that happened to it
    client_restriction_attachments   notices, emails, legal letters
    restriction_reason (master list) the categories, editable by an
                                     administrator

Seeds the reason categories, and optionally the first entry —
Walchandnagar Industries Limited — but **only when a reason is given on
the command line**. A block is a management decision with a stated
reason; a migration that invents one would be putting words in
somebody's mouth, and the reason is what every refused salesperson
reads.

    .venv/bin/python scripts/2026_10_12_client_restrictions.py --check
    .venv/bin/python scripts/2026_10_12_client_restrictions.py
    .venv/bin/python scripts/2026_10_12_client_restrictions.py \\
        --seed-wil --approver DIR12010 \\
        --category "Payment default / outstanding" \\
        --reason "Outstanding against invoices ... as at ..."
    .venv/bin/python scripts/2026_10_12_client_restrictions.py --down --yes
"""
import argparse
import os
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect                            # noqa: E402

from app import app, db                                   # noqa: E402
from app.models.restriction import (                      # noqa: E402
    BLOCKED, ClientRestriction, ClientRestrictionEvent, RestrictionAttachment)

TABLES = (
    ('client_restrictions', ClientRestriction),
    ('client_restriction_events', ClientRestrictionEvent),
    ('client_restriction_attachments', RestrictionAttachment),
)

LIST_KEY = 'restriction_reason'
CATEGORIES = [
    'Payment default / outstanding',
    'Commercial dispute',
    'Legal case',
    'Unethical conduct',
    'Safety / compliance breach',
    'Poor credit',
    'Competitor / conflict of interest',
    'Management decision',
    'Other',
]

WIL_NAME = 'Walchandnagar Industries Limited'
WIL_ALIASES = ['Walchandnagar Industries Ltd',
               'Walchandnagar Industries Limit',
               'Walchandnagar Industries',
               'WIL']
WIL_DOMAINS = ['walchand.com']


def _seed_categories(dry_run):
    from app.models.master_data import MasterItem, MasterList

    existing = {i.code for i in
                MasterItem.query.filter_by(list_key=LIST_KEY).all()}
    if not MasterList.query.filter_by(key=LIST_KEY).first():
        if dry_run:
            print(f'  WOULD register the master list {LIST_KEY!r}')
        else:
            db.session.add(MasterList(
                key=LIST_KEY, label='Restriction Reason',
                description='Why a client is blocked or on the caution '
                            'register.',
                is_system=True, sort_order=400))
    added = 0
    for order, label in enumerate(CATEGORIES):
        if label in existing:
            continue
        added += 1
        if not dry_run:
            db.session.add(MasterItem(list_key=LIST_KEY, code=label,
                                      label=label, sort_order=order * 10,
                                      is_active=True))
    print(f'  {"WOULD add" if dry_run else "added"} {added} reason '
          f'category(ies)')


def _seed_wil(args, dry_run):
    from app.services import client_restrictions as restrictions

    if ClientRestriction.query.filter_by(company_name=WIL_NAME).first():
        print(f'  {WIL_NAME} is already on the register — leaving it alone')
        return
    if not args.reason or not args.category:
        print('  NOT seeding Walchandnagar: --category and --reason are '
              'required.\n'
              '  A block is a management decision with a stated reason, '
              'and the\n'
              '  reason is what every refused salesperson reads. This '
              'script will\n'
              '  not invent one.')
        return
    if dry_run:
        print(f'  WOULD block {WIL_NAME}')
        print(f'    aliases : {", ".join(WIL_ALIASES)}')
        print(f'    domains : {", ".join(WIL_DOMAINS)} (scope: '
              f'{args.scope})')
        print(f'    category: {args.category}')
        print(f'    reason  : {args.reason[:120]}')
        print(f'    approver: {args.approver}')
        if args.scope == 'group':
            print('    NOTE: scope "group" blocks everybody on '
                  'walchand.com, which covers other Walchand companies. '
                  'Use "entity" if the decision is about Walchandnagar '
                  'Industries alone.')
        return

    row = ClientRestriction(
        status=BLOCKED,
        company_name=WIL_NAME,
        name_key=restrictions.normalise(WIL_NAME),
        reason_category=args.category,
        reason_detail=args.reason,
        recommended_by=args.approver,
        recommended_at=datetime.utcnow(),
        approved_by=args.approver,
        approved_at=datetime.utcnow(),
        scope=args.scope,
        legal_status=args.legal_status,
    )
    row.aliases = WIL_ALIASES
    row.domains = WIL_DOMAINS
    row.emails = []
    row.business_units = []
    db.session.add(row)
    db.session.flush()
    db.session.add(ClientRestrictionEvent(
        restriction_id=row.id, action='blocked', user_id=args.approver,
        note='Seeded with the register, by management decision.',
        payload_json='{"source": "migration"}'))
    print(f'  + blocked {WIL_NAME} (id {row.id})')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    ap.add_argument('--seed-wil', action='store_true',
                    help='also add Walchandnagar Industries as blocked')
    ap.add_argument('--category', help='reason category for the seed')
    ap.add_argument('--reason', help='reason detail for the seed')
    ap.add_argument('--approver', default='',
                    help='employee code of the approving manager')
    ap.add_argument('--scope', default='entity', choices=('entity', 'group'))
    ap.add_argument('--legal-status', dest='legal_status', default='None')
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        from app import Employee
        if not Employee.query.count():
            raise SystemExit('Refusing to run: not the production database.')

        insp = inspect(db.engine)
        present = {name: insp.has_table(name) for name, _ in TABLES}

        if args.down:
            have = [n for n, _ in TABLES if present[n]]
            if args.check or not have:
                print('== DRY-RUN == WOULD drop: '
                      + (', '.join(have) if have else 'nothing'))
                print('  This discards every block, every caution and the '
                      'whole history of who decided what — including the '
                      'record of attempts. Records already closed by a '
                      'block stay closed; nothing reopens them.')
                return
            if not args.yes:
                raise SystemExit('  Refusing without --yes.')
            for name, model in reversed(TABLES):
                if present[name]:
                    model.__table__.drop(bind=db.engine)
                    print(f'  - {name}')
            return

        if args.check:
            print('== DRY-RUN — nothing written ==')
            for name, _ in TABLES:
                print(f'  {"(already there)" if present[name] else "WOULD create"}'
                      f'  {name}')
            _seed_categories(True)
            if args.seed_wil:
                _seed_wil(args, True)
            else:
                print('  (no --seed-wil: no client is added)')
            return

        db.metadata.create_all(
            bind=db.engine,
            tables=[model.__table__ for name, model in TABLES
                    if not present[name]])
        for name, _ in TABLES:
            print(f'  + {name}')
        _seed_categories(False)
        if args.seed_wil:
            _seed_wil(args, False)
        db.session.commit()

        from app.services import client_restrictions as restrictions
        restrictions.cache_clear()
        print('\nDone. The register is at /CRM/admin/restrictions.')


if __name__ == '__main__':
    main()
