"""
The 08:30 check: is any open lead sitting with no owner?

    .venv/bin/python scripts/unowned_sweep.py --check
    .venv/bin/python scripts/unowned_sweep.py --apply

CRM-COCKPIT-02 §4.5. 61 open leads created between 12 and 29 October
had no owner and no activity, and were assigned by hand on 1 November.
A lead with no owner is on nobody's list: it appears in no morning
brief, trips no overdue count, and is found when somebody goes
looking.

The rule, in the prompt's own order: the account's owner for that
vertical, then the vertical head, then DIR12010. All three come from
`auto_assign`, which is the one place that decides who owns a lead —
this script is a timer and a report, not a second rule.

What it is not: a way to make the number look like zero. Every
assignment is recorded with the rule that chose it, and an assignment
by backstop (nobody else could be worked out) is reported separately,
because a pile of leads landing on DIR12010 every morning is a
different problem from a pile of leads with no owner.

The list goes into the daily brief (§4.5), so it is read rather than
sitting in a log.
"""
import argparse
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402

LEASE = 'unowned_sweep'
BATCH = 'cockpit02.unowned'
NOTE = ('01-Nov-2026 CRM-COCKPIT-02: owner set by rule at 08:30 '
        '(rule: §4.5, account owner → vertical head → DIR12010)')


def unowned():
    from app.services import sales_rules as rules
    from sqlalchemy import or_

    return (Lead.query
            .filter(Lead.is_archived.isnot(True),
                    Lead.stage.in_(rules.open_stages()),
                    or_(Lead.assigned_to.is_(None),
                        Lead.assigned_to == ''))
            .order_by(Lead.created_at).all())


def plan():
    """[(lead, code, rule)] — who each one would go to, and why."""
    from app import Company
    from app.services import auto_assign

    out, why = [], Counter()
    for lead in unowned():
        account = (db.session.get(Company, lead.company_id)
                   if lead.company_id else None)
        try:
            # (code, rule, needs_confirmation). The third is why the
            # report separates a lead that landed on the backstop from
            # one whose customer the CRM actually knows: both have an
            # owner, and only one of them is settled.
            code, rule, unsure = auto_assign.decide(
                account=account,
                vertical=lead.procam_vertical or '',
                sender_email=(lead.email or
                              lead.original_email_from or ''))
        except Exception:                                 # noqa: BLE001
            code, rule, unsure = None, 'could not be decided', True
        why[rule or 'no rule matched'] += 1
        out.append((lead, code, rule, unsure))
    return out, why


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--actor', default='CRM-COCKPIT-02')
    args = ap.parse_args()
    if not (args.check or args.apply):
        args.check = True

    with app.app_context():
        from app.services import bulk_log, leases

        print(f'database: {db.engine.url}')
        print('MODE:     ' + ('APPLY' if args.apply
                              else 'CHECK — nothing written') + '\n')

        rows, why = plan()
        print(f'  open leads with no owner       {len(rows)}')
        print('    §4.5 asks for this to be 0\n')
        for rule, n in why.most_common():
            print(f'    {rule:<46} {n}')

        if rows:
            print('\n  a sample of what would be written:')
            for lead, code, rule, unsure in rows[:15]:
                print(f'    #{lead.id:<7} {(lead.company or "")[:24]:<24} '
                      f'{(lead.procam_vertical or "-")[:16]:<16} '
                      f'-> {code or "NOBODY":<10} ({rule})')

        if args.check:
            print('\nCHECK ONLY — nothing written. --apply assigns '
                  'these;\nscripts/undo_batch.py puts them back.')
            return 0

        with leases.hold(LEASE, seconds=600) as got:
            if not got:
                print('another sweep holds the lease — skipping.')
                return 0
            from app import Employee
            from app.services import audit

            assigned, unresolved, to_confirm = 0, 0, 0
            with bulk_log.batch(BATCH, note=NOTE,
                                actor=args.actor) as log:
                for lead, code, rule, unsure in rows:
                    if not code:
                        unresolved += 1
                        continue
                    emp = Employee.query.filter_by(
                        emp_code=code).first()
                    log.set(lead, 'assigned_to', code)
                    log.set(lead, 'assigned_name',
                            getattr(emp, 'name', code))
                    audit.record('lead.ownership_change', 'lead',
                                 lead.id,
                                 old={'assigned_to': None},
                                 new={'assigned_to': code,
                                      'rule': rule,
                                      'needs_confirmation': unsure},
                                 actor=args.actor, reason=NOTE,
                                 commit=False)
                    assigned += 1
                    if unsure:
                        to_confirm += 1
                batch_name = log.name
            try:
                db.session.commit()
            except Exception as exc:                      # noqa: BLE001
                db.session.rollback()
                print(f'  !! ROLLED BACK: {type(exc).__name__}: '
                      f'{str(exc)[:200]}')
                return 1

            print(f'\n  {assigned} lead(s) given an owner.')
            if to_confirm:
                print(f'  {to_confirm} of those came from a fallback '
                      f'rather than from knowing the\n  customer, and '
                      f'are on the owner\'s confirmation list — a pile '
                      f'of leads\n  landing on the backstop every '
                      f'morning is its own problem.')
            if unresolved:
                print(f'  {unresolved} could not be decided at all — '
                      f'these stay unowned and are\n  on the list '
                      f'above. An owner the CRM invents is worse than '
                      f'a gap it reports.')
            print(f'\n  reversible log batch: {batch_name}')
            print(f'  undo: .venv/bin/python scripts/undo_batch.py '
                  f'--batch {batch_name} --apply')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
