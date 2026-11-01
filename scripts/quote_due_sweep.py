"""
The pass over quote submission deadlines.

    .venv/bin/python scripts/quote_due_sweep.py --check
    .venv/bin/python scripts/quote_due_sweep.py --apply

CRM-COCKPIT-02 §2.7. Two jobs, both on a timer because neither has a
moment that triggers it — nothing happens when a deadline passes,
which is exactly the problem:

  * **the misses.** A deadline gone by with no quote recorded and no
    Bid / Not bid decision. The lead is flagged, and the owner, the
    vertical head and the standing CC are told once. Five Indo Tech
    auctions closed in October with nothing recorded either way and
    nobody found out until somebody went looking.
  * **the reminders.** Before a deadline, at the hours in
    Admin → CRM rules (`quote_due_reminder_hours`, 48 and 24 by
    default). The 48-hour figure is the same one the "Due in 48 h"
    tile counts, so the mail and the tile cannot disagree.

Both are idempotent: `quote_due_missed_at` is stamped when the mail
goes, and the reminder's `event_key` carries the hour mark, so a
sweep that runs every hour does not mail anybody twice.

Run it hourly. A deadline is a time of day, so a daily pass would tell
somebody at 09:00 that an auction closed at 15:15 yesterday.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402

LEASE = 'quote_due_sweep'


def _utcnow():
    """Naive UTC, which is what the database stores."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _open_with_a_deadline():
    from app.services import sales_rules as rules

    return (Lead.query
            .filter(Lead.is_archived.isnot(True),
                    Lead.stage.in_(rules.open_stages()),
                    Lead.quote_due_at.isnot(None)))


def misses(now=None):
    """Leads whose deadline has passed with nothing recorded."""
    from app.services import quote_due as qd

    now = now or _utcnow()
    return [l for l in _open_with_a_deadline()
            .filter(Lead.quote_due_at <= now,
                    Lead.quote_due_missed_at.is_(None)).all()
            if qd.is_missed(l, now=now)]


def reminders(now=None):
    """[(lead, hours)] due a reminder at one of the configured marks.

    The nearest mark that has been reached and not passed by more than
    an hour: the sweep runs hourly, so a lead crossing the 48-hour
    line is caught in the hour it crosses it and not again.
    """
    from app.services import admin_settings as settings

    now = now or _utcnow()
    try:
        marks = [int(h) for h in
                 (settings.get('quote_due_reminder_hours') or [48, 24])]
    except (TypeError, ValueError):
        marks = [48, 24]
    out = []
    for lead in _open_with_a_deadline().filter(
            Lead.quote_due_at > now,
            Lead.quote_due_missed_at.is_(None)).all():
        from app.services import quote_due as qd
        if qd.is_missed(lead, now=now):
            continue
        left = (lead.quote_due_at - now).total_seconds() / 3600.0
        for mark in sorted(marks):
            if mark - 1 < left <= mark:
                out.append((lead, mark))
                break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--no-mail', action='store_true',
                    help='flag the misses without telling anybody')
    args = ap.parse_args()
    if not (args.check or args.apply):
        args.check = True

    with app.app_context():
        from app.services import leases, quote_due as qd
        from app.services import quote_due_flow as flow

        print(f'database: {db.engine.url}')
        print('MODE:     ' + ('APPLY' if args.apply
                              else 'CHECK — nothing written') + '\n')

        now = _utcnow()
        missed = misses(now)
        soon = reminders(now)
        total = _open_with_a_deadline().count()

        print(f'  open leads with a deadline     {total}')
        print(f'  deadline passed, nothing filed {len(missed)}')
        print(f'  reminders due now              {len(soon)}')

        if missed:
            print('\n  would be flagged Missed submission:')
            for lead in missed[:20]:
                print(f'    #{lead.id:<7} {(lead.company or "")[:30]:<30} '
                      f'due {qd.shown(lead.quote_due_at)}  '
                      f'owner {lead.assigned_to or "-"}')
            if len(missed) > 20:
                print(f'    ... and {len(missed) - 20} more')
        if soon:
            print('\n  reminders:')
            for lead, mark in soon[:20]:
                print(f'    #{lead.id:<7} in {mark}h  '
                      f'{(lead.company or "")[:30]:<30} '
                      f'owner {lead.assigned_to or "-"}')

        if args.check:
            print('\nCHECK ONLY — nothing written, nothing sent.')
            return 0

        with leases.hold(LEASE, seconds=600) as got:
            if not got:
                print('another sweep holds the lease — skipping.')
                return 0
            flagged, mailed = 0, 0
            for lead in missed:
                if flow.mark_missed(lead, now=now):
                    flagged += 1
            try:
                db.session.commit()
            except Exception as exc:                      # noqa: BLE001
                db.session.rollback()
                print(f'  !! ROLLED BACK: {type(exc).__name__}: '
                      f'{str(exc)[:200]}')
                return 1
            # The mail only after the flag is committed. A mail sent
            # and then rolled back tells somebody about a state the
            # CRM does not hold, and this release has spent enough
            # time on side effects that outlive their transaction.
            if not args.no_mail:
                for lead in missed:
                    flow.tell_about_miss(lead)
                    mailed += 1
                for lead, mark in soon:
                    flow.tell_about_soon(lead, mark)
                    mailed += 1
            print(f'\n  {flagged} lead(s) flagged Missed submission.')
            print(f'  {mailed} mail(s) sent.')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
