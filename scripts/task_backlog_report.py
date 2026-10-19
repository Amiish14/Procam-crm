"""
What a task-backlog clean-up would close. DRY RUN unless told twice.

    .venv/bin/python scripts/task_backlog_report.py --check

Two rules, from CRM-TASKS-04:

  (a) lead closed      — an open task on a lead that is already Won,
                         Lost, On Hold or Not Interested. The work is
                         about a deal nobody is working.
  (b) expired          — an open task older than four months whose
                         lead is also older than four months. Nothing
                         that has sat untouched for a third of a year
                         on a lead that old is going to be done now.

Rule (a) wins where both apply, because "the lead closed" is the
truer reason and the one somebody reading the row later can check.

Nothing is written. `--apply` exists and refuses without
`--i-have-approval`, because this closes thousands of rows and the
instruction was explicitly to stop and report first.
"""
import argparse
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Employee, Lead, app, db                   # noqa: E402
from app.models.task_engine import (TaskInstance,         # noqa: E402
                                    TaskInstanceStatus)
from app.services.task_autoclose import TERMINAL_STAGES   # noqa: E402

#: "Old" for both the task and the lead behind it.
MONTHS = 4
OLD_DAYS = MONTHS * 30

REASONS = {
    'lead_closed': 'lead closed',
    'expired': 'expired — lead no longer live',
}


def classify(now=None):
    """{task_id: reason} for everything the two rules would close."""
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=OLD_DAYS)

    leads = {l.id: l for l in Lead.query.all()}
    out = {}
    rows = (TaskInstance.query
            .filter(TaskInstance.status.in_(TaskInstanceStatus.OPEN))
            .all())
    for row in rows:
        lead = leads.get(row.entity_id) if row.entity_type == 'Lead' else None
        if lead is None:
            continue
        if (lead.stage or '') in TERMINAL_STAGES:
            out[row.id] = 'lead_closed'           # (a) wins
            continue
        lead_age_from = lead.created_at or lead.updated_at
        if (row.created_at and row.created_at < cutoff
                and lead_age_from and lead_age_from < cutoff):
            out[row.id] = 'expired'
    return out, rows, leads


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--i-have-approval', action='store_true')
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('MODE:     DRY RUN — nothing is written\n')

        picked, rows, leads = classify()
        by_id = {r.id: r for r in rows}
        names = {e.emp_code: e.name for e in Employee.query.all()}

        print(f'  open tasks now                  {len(rows):>6}')
        print(f'  would close                     {len(picked):>6}')
        print(f'  would remain open               {len(rows) - len(picked):>6}')
        print()
        for key, label in REASONS.items():
            n = sum(1 for v in picked.values() if v == key)
            print(f'  {label:<34}{n:>6}')

        for key, label in REASONS.items():
            ids = [i for i, v in picked.items() if v == key]
            if not ids:
                continue
            print(f'\n  === {label} ({len(ids)})')
            print('  by task:')
            for k, n in Counter(by_id[i].task_key
                                for i in ids).most_common(10):
                print(f'      {n:>5}  {k}')
            print('  by owner:')
            owners = Counter(
                (by_id[i].owner_user_id or by_id[i].owner_role or '(nobody)')
                for i in ids)
            for who, n in owners.most_common(12):
                print(f'      {n:>5}  {who}'
                      f'{"  " + names[who] if who in names else ""}')
            if len(owners) > 12:
                print(f'      … and {len(owners) - 12} more owners')

        # The ones neither rule touches, so the remainder is legible
        # rather than just a subtraction.
        rest = [r for r in rows if r.id not in picked]
        if rest:
            print(f'\n  === left open ({len(rest)})')
            for k, n in Counter(r.task_key for r in rest).most_common(10):
                print(f'      {n:>5}  {k}')

        if args.apply and not args.i_have_approval:
            print('\n  !! --apply refused. This closes thousands of rows '
                  'and the instruction was to report and stop. Re-run '
                  'with --apply --i-have-approval once that approval '
                  'exists.')
            raise SystemExit(2)
        print('\nDRY RUN ONLY — nothing was written.')


if __name__ == '__main__':
    main()
