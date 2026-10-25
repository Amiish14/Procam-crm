"""
Whoever wrote to the customer owns the account.

    .venv/bin/python scripts/owner_from_sender.py --check
    .venv/bin/python scripts/owner_from_sender.py --apply
    .venv/bin/python scripts/owner_from_sender.py --down --yes

The recovered batch linked 69 leads to real accounts, and most of
those accounts have nobody named for the lead's vertical — so the
leads are out of one queue and into another. The mail itself
answers the question: a Procam employee wrote to that customer, so
they are the relationship.

Taken only when the sender is still active. 89 of the 118 people
on file have left, and naming one of them as an account owner
would be worse than leaving the row empty: an empty row asks the
head a question, a dead owner answers it wrongly and silently.

Both the account's vertical owner and the lead's own owner are
set, because setting one without the other leaves the lead
assigned to whoever happened to receive the batch.
"""
import argparse
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Company, Employee, Lead, app, db          # noqa: E402

ACTION_OWNER = 'account.vertical_owner_from_sender'
ACTION_LEAD = 'lead.owner_from_sender'
SOURCE_NOTE = 'from the sender of our own mail to this customer'


def sender_employee(lead):
    """(employee, address, why) — who wrote the mail, if they are here."""
    from app.services import outbound_mail as om

    body = (lead.original_email_body or lead.notes or '')
    sender, _to, _subject = om.read_header(body)
    if not sender:
        return None, '', 'no From: line to read'
    emp = Employee.query.filter(
        db.func.lower(Employee.email) == sender.lower()).first()
    if emp is None:
        return None, sender, f'{sender} is not an employee on file'
    if not getattr(emp, 'is_active', True):
        return None, sender, (f'{emp.name} has left — left for the '
                              f'head rather than named')
    return emp, sender, ''


def plan():
    """[(lead, account, vertical, employee, why)] for the batch."""
    from app.models.assignment import AccountVerticalOwner
    from app.models.audit import AuditEvent
    from app.services import account_gate

    ids = [int(e.entity_id) for e in AuditEvent.query.filter_by(
        action='lead.account_recovered_from_recipient').all()
        if (e.entity_id or '').isdigit()]
    take, leave = [], []
    for lead_id in sorted(set(ids)):
        lead = db.session.get(Lead, lead_id)
        if lead is None or not lead.company_id:
            continue
        account = db.session.get(Company, lead.company_id)
        vertical = account_gate.vertical_of(lead)
        if not vertical:
            leave.append((lead, account, '', None,
                          'the lead carries no vertical'))
            continue
        if AccountVerticalOwner.query.filter_by(
                account_id=account.id, vertical=vertical).first():
            continue                      # already owned; leave it
        emp, address, why = sender_employee(lead)
        if emp is None:
            leave.append((lead, account, vertical, None, why))
        else:
            take.append((lead, account, vertical, emp, address))
    return take, leave


def apply_plan(take, *, actor='DIR12010'):
    from app.models.assignment import AccountVerticalOwner
    from app.services import audit, lead_assignment

    written = 0
    for lead, account, vertical, emp, address in take:
        if not AccountVerticalOwner.query.filter_by(
                account_id=account.id, vertical=vertical).first():
            db.session.add(AccountVerticalOwner(
                account_id=account.id, vertical=vertical,
                emp_code=emp.emp_code, source='manual',
                note=SOURCE_NOTE))
            audit.record(ACTION_OWNER, 'company', account.id,
                         new={'vertical': vertical,
                              'emp_code': emp.emp_code},
                         actor=actor,
                         reason=f'{emp.name} wrote to {address} for '
                                f'this customer')
        was = lead.assigned_to
        if was != emp.emp_code:
            try:
                lead_assignment.assign(
                    lead, primary_code=emp.emp_code, actor=None,
                    note='wrote the mail this lead came from',
                    _defer_commit=True)
            except Exception:                             # noqa: BLE001
                lead.assigned_to = emp.emp_code
            audit.record(ACTION_LEAD, 'lead', lead.id,
                         old={'assigned_to': was},
                         new={'assigned_to': emp.emp_code},
                         actor=actor,
                         reason=f'{emp.name} wrote the mail this lead '
                                f'was made from')
        written += 1
    return written


def undo(*, actor='DIR12010'):
    from app.models.assignment import AccountVerticalOwner
    from app.models.audit import AuditEvent
    from app.services import audit

    rows = AccountVerticalOwner.query.filter_by(note=SOURCE_NOTE).all()
    for row in rows:
        db.session.delete(row)
    back = 0
    for ev in AuditEvent.query.filter_by(action=ACTION_LEAD).all():
        try:
            lead = db.session.get(Lead, int(ev.entity_id))
        except (TypeError, ValueError):
            continue
        if lead is None:
            continue
        lead.assigned_to = (ev.old_value or {}).get('assigned_to')
        back += 1
    audit.record('account.owner_from_sender_reverted', 'company',
                 None, actor=actor,
                 reason=f'{len(rows)} owner row(s) and {back} lead '
                        f'owner(s) put back')
    return len(rows), back


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    ap.add_argument('--actor', default='DIR12010')
    args = ap.parse_args()
    if len([a for a in (args.check, args.apply, args.down) if a]) != 1:
        raise SystemExit('Choose one: --check, --apply or --down.')

    with app.app_context():
        print(f'database: {db.engine.url}')
        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            owners, leads = undo(actor=args.actor)
            db.session.commit()
            print(f'  {owners} owner row(s) and {leads} lead '
                  f'owner(s) put back')
            return

        print(f'MODE:     {"APPLY" if args.apply else "DRY RUN"}\n')
        take, leave = plan()
        print(f'  accounts to be owned      {len(take)}')
        print(f'  left for the head         {len(leave)}')

        print('\n  per person:')
        for name, n in Counter(
                f'{e.emp_code} {e.name}'
                for _l, _a, _v, e, _ad in take).most_common():
            print(f'    {name:<34}{n:>5}')

        if leave:
            print('\n  left for the head, and why:')
            for why, n in Counter(w for *_r, w in leave).most_common(8):
                print(f'    {n:>4}  {why}')

        print('\n  a sample of what would be written:')
        for lead, account, vertical, emp, _ad in take[:10]:
            print(f'    #{lead.id:<7}{account.name[:26]:<28}'
                  f'{vertical:<20}{emp.emp_code} {emp.name}')

        if args.check:
            print('\nDRY RUN — nothing written.')
            return

        n = apply_plan(take, actor=args.actor)
        db.session.commit()
        print(f'\n  {n} account/lead pair(s) owned, audit rows each.')


if __name__ == '__main__':
    main()
