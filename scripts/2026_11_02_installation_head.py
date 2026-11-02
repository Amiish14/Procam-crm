"""
The Installation vertical head goes back to Sethupathy Sundaram.

    .venv/bin/python scripts/2026_11_02_installation_head.py --check
    .venv/bin/python scripts/2026_11_02_installation_head.py --apply
    .venv/bin/python scripts/2026_11_02_installation_head.py --down --yes

PROCAM-DECISIONS-02NOV, confirmed people decisions: "Installation
vertical head = Sethupathy Sundaram, in CRM and TMS. Ignore the
earlier note to set Gowdhaman R as Installation head."

This undoes `scripts/2026_10_25_installation_head.py`, which wrote
EMP472012 to the same row on 25 October under D12. Both scripts are
kept. The 25 October one is guarded shut so it cannot run again and
flip the row back; its text stays because "we once thought the head
was Gowdhaman" is worth being able to read a year from now, and
because the row it wrote is what this script has to find.

What this does NOT do, for the same reasons the October script gave:

  * Gowdhaman's role stays as it is. The Account check queue opens
    for whoever is head in `vertical_head`, not by role, so the role
    is not load-bearing and changing it would alter his access
    elsewhere for no reason.

  * He keeps every Installation account he owns, and the Nippon
    Express ownership Module 2 gives him. Heading a vertical and
    serving customers in it are different jobs; reassigning them
    would move live work on the strength of a head row.

  * Sethupathy's own rights are untouched. Module 7.2 of the same
    pack says he gets no company-wide admin in TMS, and being head
    here does not grant any.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Employee, app, db                         # noqa: E402
from app.models.email_admin import VerticalHead           # noqa: E402

VERTICAL = 'Installation'

#: S Sethupathy. The vertical master's original value, and the
#: 2 November decision's.
NEW_HEAD = 'DIR52011'

#: What the 25 October run put there. Recorded so --check can say
#: "this is the row that script wrote" rather than just "it differs".
OCTOBER_HEAD = 'EMP472012'

ACTOR = 'PROCAM-DECISIONS-02NOV'
REASON = ('2 Nov 2026: the Installation head is Sethupathy Sundaram; '
          'the 24 Oct D12 note naming Gowdhaman R is withdrawn')


def _name(code):
    if not code:
        return '(nobody)'
    emp = Employee.query.filter_by(emp_code=code).first()
    return getattr(emp, 'name', '') or '(not on file)'


def _row():
    return VerticalHead.query.filter_by(vertical=VERTICAL).first()


def _report(row):
    print(f'  vertical            {VERTICAL}')
    if row is None:
        print('  !! no row in vertical_head — nothing to change')
        return
    today = row.head_emp_code or ''
    print(f'  head today          {today or "(nobody)"} '
          f'({_name(today)})')
    print(f'  head wanted         {NEW_HEAD} ({_name(NEW_HEAD)})')
    if today == OCTOBER_HEAD:
        print(f'  this is the value the 25 October script wrote')
    elif today == NEW_HEAD:
        print('  already the 2 November decision — nothing to do')
    elif today:
        print('  !! neither value this script knows about: somebody '
              'has changed it since')
    from app.models.assignment import AccountVerticalOwner
    kept = AccountVerticalOwner.query.filter_by(
        vertical=VERTICAL, emp_code=OCTOBER_HEAD).count()
    print(f'  accounts Gowdhaman owns   {kept} (unchanged by this)')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--down', action='store_true',
                    help='put back whatever was there before --apply')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()
    if len([a for a in (args.check, args.apply, args.down) if a]) != 1:
        raise SystemExit('Choose one: --check, --apply or --down.')

    with app.app_context():
        from app.services import audit, vertical_heads

        print(f'database: {db.engine.url}\n')
        row = _row()
        _report(row)
        if row is None:
            raise SystemExit('No Installation row to change.')

        if args.check:
            print('\nCHECK ONLY — nothing written.')
            return

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            from app.models.audit import AuditEvent
            ev = (AuditEvent.query
                  .filter(AuditEvent.action == 'vertical_head.changed',
                          AuditEvent.entity_id == VERTICAL,
                          AuditEvent.actor == ACTOR)
                  .order_by(AuditEvent.id.desc()).first())
            was = (getattr(ev, 'old_value', None) or {}).get('head_emp_code')
            if not was:
                raise SystemExit('No audit row from this script to '
                                 'restore from.')
            row.head_emp_code = was
            db.session.commit()
            vertical_heads.cache_clear()
            print(f'\n  put back to {was} ({_name(was)})')
            return

        if (row.head_emp_code or '') == NEW_HEAD:
            print('\n  already set — nothing to do')
            return
        # A head nobody can be is worse than the wrong head: every
        # later reader of heads_for_vertical() answers with a code
        # that resolves to no person, and the Account check queue
        # opens for nobody at all.
        if Employee.query.filter_by(emp_code=NEW_HEAD).first() is None:
            raise SystemExit(f'{NEW_HEAD} is not an employee on file.')

        was = row.head_emp_code
        row.head_emp_code = NEW_HEAD
        row.note = (f'{row.note or ""} | head set to {NEW_HEAD} per '
                    f'PROCAM-DECISIONS-02NOV').strip(' |')[:300]
        audit.record('vertical_head.changed', 'vertical_head', VERTICAL,
                     old={'head_emp_code': was},
                     new={'head_emp_code': NEW_HEAD},
                     actor=ACTOR, reason=REASON)
        db.session.commit()
        vertical_heads.cache_clear()
        print(f'\n  {VERTICAL}: {was or "(nobody)"} ({_name(was)}) -> '
              f'{NEW_HEAD} ({_name(NEW_HEAD)})')
        print('  Account check for Installation now opens for him, '
              'because this table says he is the head.')
        print('  Gowdhaman keeps his role and every account he owns.')


if __name__ == '__main__':
    main()
