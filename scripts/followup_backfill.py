"""
Give every open lead a next date.

    .venv/bin/python scripts/followup_backfill.py --check
    .venv/bin/python scripts/followup_backfill.py --apply
    .venv/bin/python scripts/undo_batch.py --batch cockpit02.followup.<date> --apply

CRM-COCKPIT-02 §3.4. Every open lead with no follow-up gets its
stage's default, counted from today, with the audit note the prompt
asks for: "follow-up set by rule 01-Nov-2026".

§3.5 then says "Follow-up missing must read 0 after back-fill and stay
0". The first half is this script. The second half is the gate in
`cockpit02_gate.py`, which is why this is a one-off and not a nightly
job: a nightly job filling the field would hide the fact that people
had stopped filling it, which is the state this release found.

Won, Lost and On Hold are skipped (§3.1). So is any lead whose owner
has already set a date, whatever that date is — a person's date is a
decision, and a back-fill that "corrects" it is a back-fill that
argues with the owner.

The dates land on working days: a rule that proposes Sunday for a
quarter of the book produces a Monday morning with three days of
follow-ups in it, and people stop believing the list.
"""
import argparse
import os
import sys
from collections import Counter
from datetime import date, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402

BATCH = 'cockpit02.followup'
NOTE = ('01-Nov-2026 CRM-COCKPIT-02: follow-up set by rule '
        '01-Nov-2026 (rule: §3.2 stage default)')


def _next_working_day(day):
    """Saturday and Sunday move to Monday."""
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def population():
    from app.services import cockpit02_gate as gate
    from app.services import sales_rules as rules

    return (Lead.query
            .filter(Lead.is_archived.isnot(True),
                    Lead.stage.in_(rules.open_stages()),
                    Lead.stage.notin_(gate.FOLLOWUP_EXEMPT),
                    Lead.followup_date.is_(None))
            .order_by(Lead.id).all())


def plan(today=None):
    """[(lead, date)] and a tally by stage."""
    from app.services import followup

    today = today or date.today()
    out, why = [], Counter()
    for lead in population():
        wanted = followup.propose(lead, today=today)
        if wanted is None:
            why['exempt stage — skipped'] += 1
            continue
        wanted = _next_working_day(wanted)
        why[f'{lead.stage or "(no stage)"} -> +'
            f'{(wanted - today).days}d'] += 1
        out.append((lead, wanted))
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
        from app.services import bulk_log, sales_rules as rules

        print(f'database: {db.engine.url}')
        print('MODE:     ' + ('APPLY' if args.apply
                              else 'CHECK — nothing written') + '\n')

        today = rules.business_today()
        open_total = Lead.query.filter(
            Lead.is_archived.isnot(True),
            Lead.stage.in_(rules.open_stages())).count()
        rows, why = plan(today)
        print(f'  open leads                     {open_total}')
        print(f'    already have a follow-up     '
              f'{open_total - len(population())}')
        print(f'    would be set by rule         {len(rows)}')
        print()
        for label, n in why.most_common():
            print(f'    {label:<34} {n}')

        if rows:
            print('\n  a sample of what would be written:')
            for lead, wanted in rows[:10]:
                print(f'    #{lead.id:<7} {(lead.stage or "")[:20]:<20} '
                      f'-> {wanted}  '
                      f'{(lead.company or "")[:24]}')

        if args.check:
            print('\nCHECK ONLY — nothing written. --apply writes '
                  'these;\nscripts/undo_batch.py puts them back.')
            return 0

        from app.services import audit

        with bulk_log.batch(BATCH, note=NOTE, actor=args.actor) as log:
            for lead, wanted in rows:
                if not log.set(lead, 'followup_date', wanted):
                    continue
                audit.record('lead.followup_backfilled', 'lead', lead.id,
                             old={'followup_date': None},
                             new={'followup_date': str(wanted),
                                  'stage': lead.stage},
                             actor=args.actor, reason=NOTE,
                             commit=False)
            batch_name, written = log.name, len(log)

        try:
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'\n  !! ROLLED BACK — nothing was written: '
                  f'{type(exc).__name__}: {str(exc)[:200]}')
            return 1

        left = len(population())
        print(f'\n  {written} follow-up date(s) set.')
        print(f'  "Follow-up missing" is now {left} '
              f'(§3.5 asks for 0).')
        print(f'\n  reversible log batch: {batch_name}')
        print(f'  undo: .venv/bin/python scripts/undo_batch.py '
              f'--batch {batch_name} --apply')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
