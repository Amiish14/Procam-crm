"""
PROCAM-DECISIONS-02NOV Module 3 — the monthly RFQ targets.

    .venv/bin/python scripts/2026_11_02_rfq_targets.py --check
    .venv/bin/python scripts/2026_11_02_rfq_targets.py --apply
    .venv/bin/python scripts/2026_11_02_rfq_targets.py --down --yes

`rfq_targets` already exists — CRM-LEAD-JOURNEY-01 §6.3 built it, and
the journey screen writes to it. This release does **not** replace it:
it adds `source` and `basis` to that table, creates
`rfq_target_history` beside it, and then seeds one target per
salesperson per vertical from the last four whole months plus 20%
(Module 3.1).

Written down because the first version of this release did replace
it, model file and all, and the test suite caught it: four tests in
test_lead_journey and test_rfq_reports went red because the column
they read was called `target_rfqs` and mine was called `target`.

Two things --check will tell you that are worth reading before
--apply:

**Which source the count came from.** Three tables could answer "how
many RFQs did this person raise" and they do not agree on this
database — the RFQ Chain's own `rfqs` is effectively empty, so a
target read from it would be zero for everybody. The service tries
`rfqs`, then leads that reached RFQ Generated, then `rfq_date`, and
names the one it used.

**Who has no history.** Those people are listed and given no target.
A target of 0 plus 20% is 0: it passes every review automatically and
looks like a number somebody chose. An administrator sets theirs on
/admin/rfq-targets, which is the screen Module 3.2 asks for.
"""
import argparse
import os
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect                            # noqa: E402

from app import app, db                                   # noqa: E402


def _cols(table):
    try:
        return {c['name'] for c in inspect(db.engine).get_columns(table)}
    except Exception:                                     # noqa: BLE001
        return set()

#: `rfq_targets` already exists (CRM-LEAD-JOURNEY-01
#: §6.3). This release adds two columns to it and the
#: history table beside it.
TABLES = ('rfq_target_history',)
COLUMNS = (('rfq_targets', 'source', 'VARCHAR(10)'),
           ('rfq_targets', 'basis', 'VARCHAR(200)'))
ACTOR = 'PROCAM-DECISIONS-02NOV'


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
        # Imported so the two tables are in db.metadata before
        # create_all. An unimported model is a table create_all does
        # not make, and the first write then fails on "no such
        # table" — which is how CRM-COCKPIT-02 lost an hour.
        from app.models import rfq_target as _models     # noqa: F401
        from app.services import rfq_targets as rt

        print(f'database: {db.engine.url}\n')
        have = set(inspect(db.engine).get_table_names())
        print(f'  {"rfq_targets":<24} '
              f'{"present" if "rfq_targets" in have else "ABSENT"} '
              f'(built by CRM-LEAD-JOURNEY-01; not replaced)')
        for table in TABLES:
            print(f'  {table:<24} '
                  f'{"present" if table in have else "ABSENT"}')
        for table, column, _ddl in COLUMNS:
            print(f'  {table + "." + column:<24} '
                  f'{"present" if column in _cols(table) else "ABSENT"}')

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            for table in reversed(TABLES):
                db.session.execute(db.text(
                    f'DROP TABLE IF EXISTS {table}'))
                print(f'  {table} dropped')
            db.session.commit()
            print('\n  The history is gone. `rfq_targets` itself is '
                  'left exactly as it is, targets and all: it is not '
                  'this release\'s table, and the two columns added '
                  'to it are nullable and cost nothing.')
            print('  Losing the history is the one thing here that '
                  'is not reversible, which is why --down needs '
                  '--yes.')
            return

        found = rt.proposals()
        print(f'\n  window      {found["from"]} to {found["to"]} '
              f'(four whole months; this month is left out)')
        print(f'  counted from  {found["source"]}')
        print(f'  period        {found["period"]:%B %Y}')

        existing = {(row.emp_code, row.vertical): row
                    for row in rt.for_period(found['period'])} \
            if set(TABLES) <= have else {}

        print(f'\n  {len(found["rows"])} target(s) the seed proposes:')
        by_vertical = {}
        for row in found['rows']:
            by_vertical.setdefault(row['vertical'], []).append(row)
        for vertical in sorted(by_vertical):
            rows = by_vertical[vertical]
            total = sum(r['target'] for r in rows)
            print(f'\n    {vertical}  —  {len(rows)} people, '
                  f'{total} RFQs/month')
            for row in sorted(rows, key=lambda r: -r['target']):
                mark = ' ' if (row['emp_code'],
                               vertical) not in existing else '='
                print(f'    {mark} {row["name"][:28]:<28} '
                      f'{row["counted"]:>4} in 4 mo  '
                      f'{row["average"]:>6.2f}/mo  '
                      f'-> {row["target"]:>3}')
        if not found['rows']:
            print('    (none — nothing in the window to read)')

        nothing = found['no_history']
        print(f'\n  {len(nothing)} salespeople with no RFQ history, '
              f'given no target:')
        for row in nothing[:30]:
            print(f'      {row["name"][:30]:<30} {row["vertical"]}')
        if len(nothing) > 30:
            print(f'      … and {len(nothing) - 30} more')
        if nothing:
            print('\n    A target of 0 + 20% is 0. Set theirs on '
                  '/admin/rfq-targets.')

        if args.check:
            print('\nCHECK ONLY — nothing written.')
            print('Send these counts to Nilesh (rule 0.3), then '
                  '--apply.')
            return

        print('\napplying\n')
        for table, column, ddl in COLUMNS:
            if column in _cols(table):
                print(f'  {table}.{column:<18} already there')
                continue
            db.session.execute(db.text(
                f'ALTER TABLE {table} ADD COLUMN {column} {ddl}'))
            print(f'  {table}.{column:<18} added')
        db.session.commit()
        db.create_all()
        have = set(inspect(db.engine).get_table_names())
        for table in TABLES:
            print(f'  {table:<24} '
                  f'{"present" if table in have else "FAILED"}')

        written = skipped = 0
        for row in found['rows']:
            was = rt.get(row['emp_code'], row['vertical'],
                         found['period'])
            if was is not None:
                # Never overwrite a target somebody has already set:
                # the seed is a starting point (Module 3.1), and a
                # re-run that resets an administrator's decision is
                # the seed overruling a person.
                skipped += 1
                continue
            rt.set_target(row['emp_code'], row['vertical'],
                          row['target'], period=found['period'],
                          actor=ACTOR, source='seeded',
                          basis=row['basis'],
                          reason=(f'Module 3.1 seed: {row["basis"]}'))
            written += 1
        db.session.commit()

        print(f'\n  targets written:  {written}')
        print(f'  already set, left alone: {skipped}')
        print(f'  no history, not given a target: {len(nothing)}')
        print('\n  Every one of those is in rfq_target_history with '
              'its basis, so "where did this number come from" has '
              'an answer.')
        print('  Edit any of them on /admin/rfq-targets.')


if __name__ == '__main__':
    main()
