"""
The open leads with nobody, and the accounts with nobody.

READ-ONLY unless `--apply`, and `--apply` touches leads only. The
accounts are listed with a suggestion for a person to approve,
because who owns a customer is not a thing to infer and write.

    .venv/bin/python scripts/fix_unowned_leads.py --check
    .venv/bin/python scripts/fix_unowned_leads.py --apply
"""
import argparse
import os
import sys
from collections import Counter
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Company, Employee, Lead, app, db          # noqa: E402

REASON = 'owner assigned by rule, CRM-AUTO-ASSIGN-01'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    if args.apply == args.check:
        raise SystemExit('Choose one: --check or --apply.')

    with app.app_context():
        from app.services import auto_assign, sales_rules as rules
        from app.services import vertical_heads as vh

        print(f'database: {db.engine.url}')
        print(f'MODE:     {"APPLY — leads will be written" if args.apply else "DRY RUN — nothing is written"}\n')

        stages = tuple(rules.open_stages())
        unowned = (Lead.query
                   .filter(Lead.is_archived.isnot(True),
                           Lead.stage.in_(stages),
                           db.or_(Lead.assigned_to.is_(None),
                                  Lead.assigned_to == ''))
                   .order_by(Lead.id).all())

        print('=' * 72)
        print(f'OPEN LEADS WITH NO OWNER — {len(unowned)}')
        print('=' * 72)
        plan = []
        for l in unowned:
            account = (db.session.get(Company, l.company_id)
                       if l.company_id else None)
            code, rule, confirm = auto_assign.decide(
                account=account, vertical=(l.procam_vertical or ''),
                sender_email=(l.email or ''))
            emp = Employee.query.filter_by(emp_code=code).first()
            plan.append((l, code, rule, confirm))
            print(f'  lead {l.id:<7} {(l.company or "?")[:30]:<30} '
                  f'{(l.procam_vertical or "(none)"):<20} -> '
                  f'{code} {(emp.name if emp else "?")[:22]:<22} [{rule}]')

        print('\n' + '=' * 72)
        owners_missing = [c for c in Company.query.all()
                          if not (getattr(c, 'pic_emp_code', '') or '').strip()]
        print(f'ACCOUNTS WITH NO OWNER — {len(owners_missing)}')
        print('=' * 72)
        print('  Listed only. Who owns a customer is not something to '
              'infer and write.\n')
        print(f'  {"account":<40} {"leads":>6}  suggested owner, and why')
        for c in owners_missing:
            leads = (Lead.query
                     .filter(Lead.company_id == c.id,
                             Lead.is_archived.isnot(True))
                     .order_by(Lead.updated_at.desc()).limit(50).all())
            owners = Counter((l.assigned_to or '').strip().upper()
                             for l in leads if (l.assigned_to or '').strip())
            if owners:
                code, n = owners.most_common(1)[0]
                emp = Employee.query.filter_by(emp_code=code).first()
                why = (f'owns {n} of its {len(leads)} leads'
                       + ('' if emp and emp.is_active else ' — BUT HAS LEFT'))
            else:
                vertical = next((l.procam_vertical for l in leads
                                 if l.procam_vertical), '')
                heads = vh.heads_for_vertical(vertical) if vertical else []
                code = heads[0] if heads else ''
                emp = Employee.query.filter_by(emp_code=code).first() \
                    if code else None
                why = (f'no lead has an owner; head of {vertical}'
                       if code else 'no leads and no vertical to go on')
            print(f'  {(c.name or "?")[:40]:<40} {len(leads):>6}  '
                  f'{code:<12} {(emp.name if emp else ""):<24} {why}')

        if args.check:
            print('\nDRY RUN — nothing written. Re-run with --apply to '
                  'set the lead owners. The accounts are never written '
                  'by this script.')
            return

        from app.services import lead_assignment
        written = 0
        try:
            for lead, code, rule, confirm in plan:
                ok, err = lead_assignment.assign(
                    lead, primary_code=code, actor='CRM-AUTO-ASSIGN-01',
                    note=f'{REASON} — {rule}', _defer_commit=True)
                if not ok:
                    print(f'  refused for lead {lead.id}: {err}')
                    continue
                auto_assign._record(lead, code, rule, None, None,
                                    lead.email or '')
                if confirm:
                    auto_assign.open_confirmation(lead, code, rule)
                written += 1
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'\n  !! ROLLED BACK: {type(exc).__name__}: '
                  f'{str(exc)[:200]}')
            raise SystemExit(1)
        print(f'\n  {written} lead(s) given an owner, in one transaction. '
              f'Accounts untouched.')


if __name__ == '__main__':
    main()
