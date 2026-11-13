"""Can each vertical head change the PICs on their own leads? READ ONLY.

CRM-FINAL-13NOV item 14: "confirm it on live with a check script that
prints, for each vertical head, may_set_people on 3 sample leads".

    .venv/bin/python scripts/2026_11_13_pic_rights_check.py --check
    .venv/bin/python scripts/2026_11_13_pic_rights_check.py --check --leads 5

WHAT IT ASKS, AND WHY A LEAD THAT ALREADY HAS AN OWNER IS THE POINT

`lead_people.may_set_people` (dc17cd4) admits a CRM administrator, the
head of the lead's vertical, the RFQ desk, and the lead's own owner
handing it on. Item 14 is about the second of those, on the case that
is easy to get wrong: **a lead that already has an owner**.

An unowned lead is the obvious case and the one everybody tests. A
lead with an owner is where a rule written as "the owner, or an
admin" quietly excludes the head -- so for each head this prints
three leads in their vertical, chosen to include one that is already
owned by somebody else, and says yes or no and on what grounds.

The heads come from the vertical master, which is where the business
maintains them, not from a role column somebody forgot to update.

IT WRITES NOTHING. No --apply. It reads the lead, asks the rule, and
prints the answer.
"""
from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Employee, Lead, app, db                    # noqa: E402


def _rule(n=72):
    print('─' * n)


def heads():
    """[(code, vertical)] from the vertical master."""
    out = []
    try:
        from app.models.email_admin import VerticalHead
        rows = VerticalHead.query.filter_by(active=True).all()
    except Exception as exc:                              # noqa: BLE001
        print(f'  could not read the vertical master '
              f'({type(exc).__name__})')
        return out
    for row in rows:
        for code in ((row.head_emp_code or '').strip().upper(),
                     (row.deputy_emp_code or '').strip().upper()):
            if code and (code, row.vertical) not in out:
                out.append((code, row.vertical))
    return out


def sample_leads(vertical, limit=3):
    """Leads in this vertical, chosen to include the awkward ones.

    One already owned by somebody else, one unowned, one of whatever
    else there is -- because "can a head change the PIC" has a
    different answer per rule depending on which of those it is, and a
    sample of three all in the same state would prove one third of it.
    """
    from app.services import account_gate, sales_rules

    rows = (Lead.query
            .filter(Lead.is_archived.isnot(True),
                    Lead.junk_deleted.isnot(True),
                    Lead.merged_into_id.is_(None),
                    Lead.stage.in_(sales_rules.open_stages()))
            .order_by(Lead.id.desc()).limit(4000).all())
    mine = [lead for lead in rows
            if (account_gate.vertical_of(lead) or '').strip().lower()
            == (vertical or '').strip().lower()]
    owned = [l for l in mine if (l.assigned_to or '').strip()]
    unowned = [l for l in mine if not (l.assigned_to or '').strip()]
    picked = []
    if owned:
        picked.append(owned[0])
    if unowned:
        picked.append(unowned[0])
    for lead in mine:
        if len(picked) >= limit:
            break
        if lead not in picked:
            picked.append(lead)
    return picked[:limit], len(mine)


def report(limit=3):
    from app.services import lead_people

    people = heads()
    print(f'\n══ {len(people)} head/deputy seat(s) in the vertical '
          f'master ══')
    if not people:
        print('  Nobody is named as a head. Then this rule admits only '
              'administrators, the RFQ desk and the lead\'s own owner, '
              'and that is a configuration answer rather than a bug.')
        return

    for code, vertical in people:
        person = Employee.query.filter_by(emp_code=code).first()
        state = ('not in the employee master' if person is None
                 else ('active' if getattr(person, 'is_active', False)
                       else 'INACTIVE'))
        print(f'\n══ {code} · head of {vertical} · {state} ══')
        if person is None or not getattr(person, 'is_active', False):
            print('  Nothing to ask: an inactive account is refused at '
                  'sign-in before any of this is read.')
            continue
        leads, total = sample_leads(vertical, limit=limit)
        print(f'  {total} open lead(s) in {vertical}; '
              f'{len(leads)} sampled')
        if not leads:
            print('  No open leads in that vertical to ask about.')
            continue
        for lead in leads:
            allowed, why = lead_people.may_set_people(lead, code)
            owner = (lead.assigned_to or '').strip() or 'nobody'
            print(f'    #{lead.id:<7} owner {owner:<12} '
                  f'{"MAY" if allowed else "may NOT":<8} '
                  f'{(why if allowed else why[:60])}')
        # And the control: somebody from another vertical.
        other = _somebody_else(vertical)
        if other:
            allowed, _why = lead_people.may_set_people(leads[0], other)
            print(f'    control: {other} (another vertical) '
                  f'{"MAY" if allowed else "may NOT"} — expected: may NOT')


def _somebody_else(vertical):
    """An active person whose own vertical is a different one."""
    from app.services import lead_people

    want = (vertical or '').strip().lower()
    for person in Employee.query.filter_by(is_active=True).all():
        theirs = (getattr(person, 'vertical', '') or '').strip().lower()
        if not theirs or theirs == want:
            continue
        code = (person.emp_code or '').strip().upper()
        if not code:
            continue
        # Not an administrator and not the desk, or the control proves
        # nothing.
        try:
            from app.access.service import effective
            _scope, perms = effective(code)
            if any(p in perms for p in lead_people.ADMIN_PERMS):
                continue
        except Exception:                                 # noqa: BLE001
            continue
        if code in lead_people.desk_codes(vertical):
            continue
        return code
    return ''


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true', required=True,
                    help='required: this script only ever reads')
    ap.add_argument('--leads', type=int, default=3)
    args = ap.parse_args(argv)

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('CRM-FINAL-13NOV item 14 — may each head change the PICs '
              'on their own leads. READ ONLY.')
        _rule()
        report(limit=args.leads)
        print()
        db.session.rollback()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
