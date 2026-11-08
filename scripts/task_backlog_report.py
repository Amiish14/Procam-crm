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

  (c) named          — a task id given on the command line with
                         `--task`. Neither rule has to pick it; the
                         person running the sweep said to close it.

Rule (a) wins where both apply, because "the lead closed" is the
truer reason and the one somebody reading the row later can check.
A named task keeps whichever rule already picked it, so the audit
row says why the row qualified rather than only that somebody typed
its number.

`--task` exists because the four ids in CRM-FINAL-CLOSEOUT-01 §9
came off a report, not out of these rules, and "included" has to
mean included whatever `classify()` thinks. `--check` prints one
line per named id — what it is, whose it is, and whether a rule
already covers it — so the four can be read before anything closes.

A named id that is not an open task stops the run rather than being
skipped quietly: closing nine hundred rows while silently dropping
one of the four somebody asked for would report success for a job
half done.

`--check` writes nothing. `--apply` refuses without
`--i-have-approval`, because this closes a thousand rows and the
first instruction was to report and stop.

Closed rows are marked `Cancelled`, not `Completed` — nobody did
them. The rule that picked each one goes into `notes`, so a row still
says why it stopped, and each gets an audit row naming the rule.
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
    'named': 'named on the command line',
}

#: What goes into the task's own notes, so the row explains itself
#: without anybody having to find this script.
NOTES = {
    'lead_closed': ('Closed in the 19 October backlog clean-up: the '
                    'lead is already Won, Lost, On Hold or Not '
                    'Interested.'),
    'expired': ('Closed in the 19 October backlog clean-up: open for '
                'more than four months on a lead that is no longer '
                'live.'),
    'named': ('Closed in the backlog clean-up: named for closure by '
              'the person running the sweep.'),
}


def parse_ids(raw):
    """--task values to a list of ints, in the order they were given.

    Accepts `--task 1342 --task 1541` and `--task 1342,1541` both,
    because an id list copied out of a report arrives comma-separated
    and retyping it is how one of them gets dropped.
    """
    out = []
    for item in (raw or []):
        for part in str(item).replace(' ', ',').split(','):
            if not part:
                continue
            try:
                number = int(part)
            except ValueError:
                raise SystemExit(f'--task wants task ids; got {part!r}')
            if number not in out:
                out.append(number)
    return out


def classify(now=None, named=()):
    """{task_id: reason} for everything the sweep would close.

    `named` ids are added to whatever the two rules found. Where a
    rule already picked one, that rule's reason is kept: it is the
    truer answer and the one a person reading the audit row can
    check for themselves.
    """
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

    open_ids = {r.id for r in rows}
    for tid in named:
        if tid in open_ids:
            out.setdefault(tid, 'named')
    return out, rows, leads


def named_report(named, rows, leads, picked):
    """One line per --task id: what it is and who already covers it.

    Written as data rather than printed here so the same answer can
    be asserted in a test and shown on the screen.
    """
    by_id = {r.id: r for r in rows}
    out = []
    for tid in named:
        row = by_id.get(tid)
        if row is None:
            closed = TaskInstance.query.get(tid)
            out.append({
                'id': tid, 'found': closed is not None,
                'open': False,
                'state': ((f'already {closed.status}')
                          if closed is not None
                          else 'no such task'),
                'task_key': (closed.task_key if closed is not None
                             else ''),
                'owner': '', 'lead_id': None, 'stage': '',
                'rule': '',
            })
            continue
        lead = (leads.get(row.entity_id)
                if row.entity_type == 'Lead' else None)
        out.append({
            'id': tid, 'found': True, 'open': True,
            'state': row.status,
            'task_key': row.task_key or '',
            'owner': (row.owner_user_id or row.owner_role or '(nobody)'),
            'lead_id': row.entity_id,
            'stage': (lead.stage or '') if lead is not None else '',
            'rule': picked.get(tid, ''),
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--i-have-approval', action='store_true')
    ap.add_argument('--task', action='append', default=None,
                    metavar='ID',
                    help='also close this task id, whatever the two '
                         'rules say. Repeatable, and a comma-separated '
                         'list is accepted.')
    args = ap.parse_args()

    named = parse_ids(args.task)
    writing = bool(args.apply and args.i_have_approval)
    with app.app_context():
        print(f'database: {db.engine.url}')
        print(f'MODE:     '
              f'{"APPLY" if writing else "DRY RUN — nothing is written"}\n')

        picked, rows, leads = classify(named=named)
        by_id = {r.id: r for r in rows}
        names = {e.emp_code: e.name for e in Employee.query.all()}

        missing = []
        if named:
            lines = named_report(named, rows, leads, picked)
            missing = [r for r in lines if not r['open']]
            print(f'  === named with --task ({len(lines)})')
            for r in lines:
                if not r['open']:
                    print(f'      !! {r["id"]:<7} {r["state"]}'
                          + (f'  ({r["task_key"]})' if r['task_key']
                             else ''))
                    continue
                why = (REASONS.get(r['rule'], r['rule'])
                       if r['rule'] else 'no rule — closed because named')
                print(f'      {r["id"]:<7} {r["task_key"][:28]:<28} '
                      f'owner={r["owner"]:<10} '
                      f'lead #{r["lead_id"]} ({r["stage"] or "?"})')
                print(f'              → {why}')
            print()

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

        if missing:
            print(f'\n  !! {len(missing)} of the {len(named)} id(s) given '
                  f'with --task {"is" if len(missing) == 1 else "are"} not '
                  f'an open task: '
                  f'{", ".join(str(r["id"]) for r in missing)}.')
            print('     The sweep stops here rather than closing the '
                  'rest and leaving that out of the count. Drop the id '
                  'from --task, or find out what happened to it first.')
            raise SystemExit(2)

        if args.apply and not args.i_have_approval:
            print('\n  !! --apply refused. This closes a thousand rows '
                  'and the instruction was to report and stop. Re-run '
                  'with --apply --i-have-approval once that approval '
                  'exists.')
            raise SystemExit(2)
        if not writing:
            print('\nDRY RUN ONLY — nothing was written.')
            return

        from app.services import audit
        now = datetime.utcnow()
        try:
            for tid, rule in picked.items():
                row = by_id[tid]
                was = row.status
                note = NOTES[rule]
                row.status = TaskInstanceStatus.CANCELLED
                row.completed_at = now
                row.notes = ((row.notes + '\n') if row.notes else '') + note
                audit.record('task.backlog_closed', 'task_instance',
                             str(row.id), old={'status': was},
                             new={'status': TaskInstanceStatus.CANCELLED,
                                  'rule': rule,
                                  'lead_id': row.entity_id},
                             actor='CRM-TASKS-04', reason=note[:200])
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'\n  !! ROLLED BACK: {type(exc).__name__}: '
                  f'{str(exc)[:200]}')
            raise SystemExit(1)

        left = TaskInstance.query.filter(
            TaskInstance.status.in_(TaskInstanceStatus.OPEN)).count()
        print(f'\n  {len(picked)} task(s) closed, in one transaction, '
              f'each with an audit row naming the rule that picked it.')
        print(f'  open tasks now: {left}')
        print(f'\n  Of those {left}, the two definitions that can never '
              f'close account for most — see fault 3 in '
              f'RELEASE_2026_10_TASKS.md. This clean-up removed the '
              f'tasks on dead leads; it did not stop more being made.')


if __name__ == '__main__':
    main()
