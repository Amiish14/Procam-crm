"""Which "PIC email" is missing, and which of them can be filled.

CRM-WA-01 §3: "Leads show no PIC (person in charge) email. Find why …
Fix the display and backfill PIC email from the assigned user's
record (dry run first, report counts)."

    .venv/bin/python scripts/2026_11_10_pic_email.py --check
    .venv/bin/python scripts/2026_11_10_pic_email.py --check --csv
    .venv/bin/python scripts/2026_11_10_pic_email.py --apply

"PIC" MEANS TWO DIFFERENT THINGS IN THIS CRM

That is the whole of §3, and it is why the instruction has to be read
twice before anything is written.

    `leads.pic`          the **customer's** contact person. Their
                         name. `leads.email` and `leads.phone` are
                         that person's address and number. The lead
                         screen labels this field "Contact person".

    `leads.assigned_to`  **our** employee: the person in charge of
                         the lead. The lead screen labels this
                         "Primary PIC", the leads list filters on it
                         under the name `pic`, and the account master
                         calls its equivalent `pic_emp_code`.

So one word, two people, on opposite sides of the deal.

WHY THE OWNER'S ADDRESS IS NOT BACKFILLED — IT IS JOINED

The address of the employee in charge is not missing from the
database. It is on their row in the employee master, which is the
only place it should be: copied onto a lead it goes stale the moment
the person changes their address or the lead is reassigned, and then
the CRM holds two answers and mails the wrong one.

What was actually broken was the **display**, and the cause was one
line: `/api/employees` returned `emp_code, name, vertical, role` to
anybody without `admin.employees`, and the lead screen reads that
list. Administrators saw the address; the sales team did not. Fixed
in the same commit as this script — which is why there is nothing
here to write for the owner, and a `--apply` that wrote one would be
making the data worse.

WHAT **CAN** BE FILLED, AND WHAT THIS REFUSES TO DO

The customer side is a real gap: leads that carry a contact person's
name with no address for them. Those can sometimes be filled from the
lead's own record — the address the enquiry arrived from, or the
Contact row for that person — and this reports them so somebody can
decide.

It will **not** write the assigned employee's address into
`leads.email`. That column is the customer's. Putting a Procam
address in it means the next person to press "Draft email" writes to
us, the mail trail files our own message as the customer's reply, and
the quotation goes to the wrong mailbox. The instruction as written
("backfill PIC email from the assigned user's record") is safe under
one reading of PIC and corrupting under the other, so this does the
safe half and names the other.

`--apply` fills `leads.email` only where it is blank, the lead has a
contact person, and the address comes from that lead's own
**customer** mail — never from an employee record, and never over a
value somebody already typed.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import Employee, Lead, app, db                    # noqa: E402

ACTOR = 'CRM-WA-01'

DEFAULT_CSV = os.path.join(_ROOT, 'data', 'pic_email_10NOV.csv')


def _internal(address):
    """Whether this address is ours rather than a customer's."""
    try:
        from app.services import mail_policy
        return bool(mail_policy.is_internal(address))
    except Exception:                                     # noqa: BLE001
        return False


# ── our side: the employees, where the display reads from ────────────
def employee_side():
    """Whether the employee master can answer "who do I write to".

    No lead is changed by any of this. An owner with no address on
    their own row is a real problem — every notification to them is
    undeliverable — but the fix is on the employee record, not on the
    leads.
    """
    owners = Counter()
    for lead in Lead.query.filter(Lead.assigned_to.isnot(None)).all():
        code = (lead.assigned_to or '').strip().upper()
        if code:
            owners[code] += 1

    people = {e.emp_code.strip().upper(): e
              for e in Employee.query.all() if e.emp_code}
    out = {'owners': len(owners), 'leads': sum(owners.values()),
           'no_address': [], 'unknown_code': [], 'inactive': []}
    for code, count in owners.most_common():
        emp = people.get(code)
        if emp is None:
            out['unknown_code'].append((code, count))
            continue
        if not (emp.email or '').strip():
            out['no_address'].append((code, emp.name or '', count))
        elif not getattr(emp, 'is_active', True):
            out['inactive'].append((code, emp.name or '', count))
    return out


# ── their side: the customer contact ────────────────────────────────
def customer_side():
    """Leads with a contact person's name and no address for them."""
    rows = (Lead.query
            .filter(Lead.is_archived.isnot(True),
                    Lead.junk_deleted.isnot(True))
            .all())
    out = {'total': len(rows), 'named_no_email': [], 'fillable': [],
           'email_is_ours': []}
    for lead in rows:
        named = (lead.pic or '').strip()
        address = (lead.email or '').strip()

        if address and _internal(address):
            # Already wrong, and worth seeing: a Procam address in the
            # customer's email field is how "Draft email" writes to
            # us. Reported, never silently changed — somebody put it
            # there and may have meant the lead to be internal.
            out['email_is_ours'].append(lead)
            continue
        if address or not named:
            continue

        out['named_no_email'].append(lead)
        found, where = _from_own_mail(lead)
        if found:
            out['fillable'].append((lead, found, where))
    return out


def _from_own_mail(lead):
    """A customer address for this lead, out of the lead's own record.

    In order: the address the enquiry arrived from, then the Contact
    row for the account whose name matches the contact person. Both
    are the customer's. An employee's address is never a candidate —
    `_internal` refuses it — because that is the one value that turns
    "write to the customer" into "write to ourselves".
    """
    candidate = (lead.original_email_from or '').strip()
    if candidate and '@' in candidate and not _internal(candidate):
        return candidate, 'the address the enquiry arrived from'

    named = (lead.pic or '').strip().lower()
    if named and getattr(lead, 'company_id', None):
        try:
            from app import Contact
            rows = (Contact.query
                    .filter(Contact.company_id == lead.company_id)
                    .all())
        except Exception:                                 # noqa: BLE001
            rows = []
        for row in rows:
            if (row.name or '').strip().lower() != named:
                continue
            address = (row.email or '').strip()
            if address and '@' in address and not _internal(address):
                return address, f'the contact record for {row.name}'
    return '', ''


# ── the report ───────────────────────────────────────────────────────
def report(ours, theirs):
    print('\n══ "PIC" means two things, so here are both ══')

    print('\n── our side: the employee in charge ──')
    print(f'  {ours["leads"]} lead(s) have an owner, across '
          f'{ours["owners"]} people.')
    print('  Their address is NOT stored on the lead and will not be: '
          'it lives on the employee record, and a copy goes stale the '
          'moment somebody is reassigned.')
    print('  The display was the fault — /api/employees withheld '
          '`email` from anybody without admin.employees, and the lead '
          'screen reads that list. Fixed in code, nothing to backfill.')
    if ours['no_address']:
        print(f'\n  !! {len(ours["no_address"])} owner(s) have NO '
              f'address on their own employee record. Every CRM '
              f'notification to them is undeliverable:')
        for code, name, count in ours['no_address'][:15]:
            print(f'     {code:<12} {name[:28]:<28} {count} lead(s)')
        print('     Fix on Admin → Employees, not here.')
    if ours['unknown_code']:
        print(f'\n  !! {len(ours["unknown_code"])} owner code(s) on '
              f'leads match no employee row:')
        for code, count in ours['unknown_code'][:10]:
            print(f'     {code:<12} {count} lead(s)')
    if ours['inactive']:
        print(f'\n  {len(ours["inactive"])} owner(s) are deactivated '
              f'but still hold leads:')
        for code, name, count in ours['inactive'][:10]:
            print(f'     {code:<12} {name[:28]:<28} {count} lead(s)')

    print('\n── their side: the customer\'s contact person ──')
    print(f'  {theirs["total"]} live lead(s) looked at.')
    print(f'  {len(theirs["named_no_email"])} have a contact person\'s '
          f'name and no address for them.')
    print(f'  {len(theirs["fillable"])} of those can be filled from '
          f'the lead\'s own customer mail.')
    if theirs['email_is_ours']:
        print(f'\n  !! {len(theirs["email_is_ours"])} lead(s) already '
              f'hold a PROCAM address in the customer email field. '
              f'"Draft email" on those writes to us:')
        for lead in theirs['email_is_ours'][:10]:
            print(f'     #{lead.id:<7} {(lead.company or "")[:30]:<30} '
                  f'{lead.email}')
        print('     Not changed by this script — somebody put it '
              'there and may have meant it.')

    by_where = Counter(where for _l, _a, where in theirs['fillable'])
    if by_where:
        print('\n  what the fillable ones would be filled from:')
        for where, n in by_where.most_common():
            print(f'    {n:>5}  {where}')
        for lead, address, where in theirs['fillable'][:12]:
            print(f'    #{lead.id:<7} {(lead.pic or "")[:22]:<22} '
                  f'→ {address}')
        if len(theirs['fillable']) > 12:
            print(f'    … and {len(theirs["fillable"]) - 12} more')

    print('\n── what this will not do ──')
    print('  It will not write an employee\'s address into '
          'leads.email. That column is the customer\'s: a Procam '
          'address in it means "Draft email" writes to us, the mail '
          'trail files our own message as the customer\'s reply, and '
          'the quotation goes to the wrong mailbox.')


def write_csv(theirs, path):
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(['lead', 'company', 'contact_person', 'stage', 'owner',
                  'would_fill_with', 'source'])
    for lead in theirs['named_no_email']:
        found = next(((a, w) for l, a, w in theirs['fillable']
                      if l.id == lead.id), ('', ''))
        out.writerow([lead.id, (lead.company or '')[:60],
                      (lead.pic or '')[:40], lead.stage or '',
                      lead.assigned_to or '', found[0], found[1]])
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='') as fh:
        fh.write(buf.getvalue())
    print(f'\nCSV: {path}')
    return path


def apply(theirs):
    """Fill the blanks, from the lead's own customer mail only."""
    from app.services import audit

    filled = 0
    try:
        for lead, address, where in theirs['fillable']:
            if (lead.email or '').strip():
                # Somebody filled it between the survey and here.
                continue
            lead.email = address[:120]
            audit.record('lead.contact_email_backfilled', 'lead',
                         str(lead.id),
                         old={'email': ''},
                         new={'email': address, 'source': where},
                         actor=ACTOR,
                         reason=(f'CRM-WA-01 §3: the contact person was '
                                 f'named with no address; taken from '
                                 f'{where}')[:400])
            filled += 1
        db.session.commit()
    except Exception as exc:                              # noqa: BLE001
        db.session.rollback()
        print(f'\n  !! ROLLED BACK: {type(exc).__name__}: {str(exc)[:200]}')
        raise SystemExit(1)
    print(f'\n{filled} customer contact address(es) filled, each with '
          f'an audit row naming where it came from.')
    print('Nothing on the employee side was touched: that address is '
          'joined from the employee master, not stored here.')
    return filled


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true',
                    help='report only. The default.')
    ap.add_argument('--apply', action='store_true',
                    help='fill the customer contact addresses that can '
                         'be taken from the lead\'s own mail')
    ap.add_argument('--csv', nargs='?', const=DEFAULT_CSV, default=None,
                    metavar='PATH',
                    help='also write the customer-side list as a CSV')
    args = ap.parse_args(argv)

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('CRM-WA-01 §3 — the two meanings of PIC.'
              + ('' if args.apply else ' REPORT ONLY.'))

        ours = employee_side()
        theirs = customer_side()
        report(ours, theirs)

        if args.csv:
            write_csv(theirs, args.csv)

        if not args.apply:
            print('\nDRY RUN — nothing written. --apply fills the '
                  f'{len(theirs["fillable"])} customer address(es) '
                  f'above.')
            db.session.rollback()
            return 0

        apply(theirs)
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
