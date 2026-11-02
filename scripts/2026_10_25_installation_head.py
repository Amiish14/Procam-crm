"""
D12 — the Installation vertical head.

    .venv/bin/python scripts/2026_10_25_installation_head.py --check
    .venv/bin/python scripts/2026_10_25_installation_head.py --apply
    .venv/bin/python scripts/2026_10_25_installation_head.py --down --yes

SUPERSEDED on 2 November 2026 — this script no longer runs.

PROCAM-DECISIONS-02NOV says: "Installation vertical head =
Sethupathy Sundaram, in CRM and TMS. Ignore the earlier note to set
Gowdhaman R as Installation head." Use
`scripts/2026_11_02_installation_head.py`, which puts the row back
to DIR52011.

The file is kept, and guarded shut rather than deleted, for two
reasons. The row this script wrote on 25 October is what the new
script has to find, so its value belongs somewhere readable. And a
one-line script that still runs is a script somebody runs: a
re-run here would flip the head back to EMP472012 without saying
that it had contradicted a standing decision.

What it did when it ran
-----------------------
The vertical master named Sethupathy Sundaram (DIR52011); D12, on
24 October, said Gowdhaman Rajakrishnan (EMP472012). This is a row
in `vertical_head`, which is Admin-maintained data, so it changed
here and not in code.

Two things it deliberately does not do, decided 25 October:

  * his role stays `presales`. The Account check queue opens for
    whoever is head in this table, not by role, so the role is not
    load-bearing and changing it would alter his access elsewhere
    for no reason;

  * he keeps the six Installation accounts he already owns. Being
    head of a vertical and serving customers in it are different
    jobs, and reassigning them would move live work.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Employee, app, db                         # noqa: E402
from app.models.email_admin import VerticalHead           # noqa: E402

VERTICAL = 'Installation'
NEW_HEAD = 'EMP472012'


def _name(code):
    emp = Employee.query.filter_by(emp_code=code).first()
    return getattr(emp, 'name', '') or '(not on file)'


def _row():
    return VerticalHead.query.filter_by(vertical=VERTICAL).first()


def _report(row):
    print(f'  vertical            {VERTICAL}')
    if row is None:
        print('  !! no row in vertical_head')
        return
    print(f'  head today          {row.head_emp_code} '
          f'({_name(row.head_emp_code)})')
    print(f'  head wanted         {NEW_HEAD} ({_name(NEW_HEAD)})')
    from app.models.assignment import AccountVerticalOwner
    owned = AccountVerticalOwner.query.filter_by(
        vertical=VERTICAL, emp_code=NEW_HEAD).count()
    print(f'  accounts he owns    {owned} (unchanged by this)')


#: Why this script refuses. Not a comment: the guard below prints it.
SUPERSEDED = (
    'This script is superseded. PROCAM-DECISIONS-02NOV (2 Nov 2026) '
    'makes Sethupathy Sundaram the Installation head and withdraws '
    'the 24 Oct D12 note naming Gowdhaman R.\n'
    'Run instead:  scripts/2026_11_02_installation_head.py --check')


def main():
    # A guard, not a comment, and before the argument parsing so that
    # every form of the command stops here — including --down, which
    # would otherwise look like the safe way to undo this and would
    # in fact restore whatever the row held before 25 October.
    raise SystemExit(SUPERSEDED)


def _main_as_it_ran():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--down', action='store_true')
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
                          AuditEvent.entity_id == VERTICAL)
                  .order_by(AuditEvent.id.desc()).first())
            was = (getattr(ev, 'old_value', None) or {}).get('head_emp_code')
            if not was:
                raise SystemExit('No audit row to restore from.')
            row.head_emp_code = was
            db.session.commit()
            vertical_heads.cache_clear()
            print(f'\n  put back to {was} ({_name(was)})')
            return

        if (row.head_emp_code or '') == NEW_HEAD:
            print('\n  already set — nothing to do')
            return
        if Employee.query.filter_by(emp_code=NEW_HEAD).first() is None:
            raise SystemExit(f'{NEW_HEAD} is not an employee on file.')

        was = row.head_emp_code
        row.head_emp_code = NEW_HEAD
        row.note = (f'{row.note or ""} | head changed to {NEW_HEAD} '
                    f'per D12, 25 Oct').strip(' |')[:300]
        audit.record('vertical_head.changed', 'vertical_head', VERTICAL,
                     old={'head_emp_code': was},
                     new={'head_emp_code': NEW_HEAD},
                     actor='CRM-ACCOUNT-FIRST-01',
                     reason='D12: the Installation head is Gowdhaman '
                            'Rajakrishnan')
        db.session.commit()
        vertical_heads.cache_clear()
        print(f'\n  {VERTICAL}: {was} ({_name(was)}) -> '
              f'{NEW_HEAD} ({_name(NEW_HEAD)})')
        print('  His role is unchanged, and he keeps the accounts he '
              'owns. Account check opens for him because this table '
              'says he is the head.')


if __name__ == '__main__':
    main()
