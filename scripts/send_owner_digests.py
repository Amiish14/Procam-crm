"""
The morning owner-confirmation digests.

One mail per head listing the leads a rule gave them, and one to the
backstop listing anything unanswered for more than two days.

    .venv/bin/python scripts/send_owner_digests.py --dry-run
    .venv/bin/python scripts/send_owner_digests.py

Run by a timer alongside the 08:30 brief. With FEATURE_AUTO_ASSIGN
off it sends nothing and says so.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import app, db                                   # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    with app.app_context():
        from app.services import flags, owner_digest

        print(f'database: {db.engine.url}')
        live = flags.on('FEATURE_AUTO_ASSIGN')
        if not live and not args.dry_run:
            print('FEATURE_AUTO_ASSIGN is off — nothing to send.')
            return
        if not live:
            # A dry run still counts. Being told only that the flag is
            # off answers the one question a dry run is not being
            # asked: what would this do if it ran?
            print('FEATURE_AUTO_ASSIGN is off — this would send nothing. '
                  'Counting anyway, so you can see what is waiting.\n')

        waiting = owner_digest.pending()
        stale = owner_digest.pending(
            older_than_hours=owner_digest.ESCALATE_AFTER_HOURS)
        people = {}
        for c in waiting:
            people.setdefault(c.emp_code, 0)
            people[c.emp_code] += 1

        print(f'  {len(waiting)} confirmation(s) waiting, '
              f'{len(people)} person/people')
        for code, n in sorted(people.items(), key=lambda kv: -kv[1]):
            print(f'      {code:<14} {n:>4}')
        print(f'  {len(stale)} older than '
              f'{owner_digest.ESCALATE_AFTER_HOURS}h — would be raised '
              f'with the backstop')

        if args.dry_run:
            print('\nDRY RUN — nothing queued.')
            return

        report = owner_digest.run()
        print(f'\n  {report["sent"]} digest(s) queued: '
              f'{", ".join(report["people"]) or "none"}')
        if report['escalated']:
            print(f'  escalation queued for {report["escalated"]} lead(s)')
        if report['skipped']:
            print(f'  skipped: {report["skipped"]}')


if __name__ == '__main__':
    main()
