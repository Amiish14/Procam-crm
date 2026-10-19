"""
Retire the failed outbox rows that predate the SMTP fix.

    .venv/bin/python scripts/cancel_stale_failed_mail.py --check
    .venv/bin/python scripts/cancel_stale_failed_mail.py --apply

156 messages failed between 4 and 12 October with no SMTP code and
the same useless error — "the mail server refused it" — because the
CRM had no working sender until the 13th. They describe work from
two weeks ago.

They are marked `cancelled`, not deleted and never resent. Deleted
loses the evidence that the message existed at all; resent would
put a fortnight of stale alerts into people's inboxes in one
morning, which is the opposite of the problem this fixed.

Only rows that carry no SMTP code and predate the cutoff: anything
that failed since then was graded by a mail server that told us
why, and those are a live problem rather than history.
"""
import argparse
import os
import sys
from collections import Counter
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import app, db                                   # noqa: E402

#: The day SMTP started working. Anything that failed before it
#: failed for want of a sender, not for want of a fix.
CUTOFF = datetime(2026, 10, 13)

NOTE = ('Stale: queued before the 13 Oct SMTP fix. Not resent — see '
        'RELEASE_2026_10_MAIL_VISIBILITY.')


def stale_rows(cutoff=CUTOFF):
    """The rows this script would cancel, under one definition.

    A function and not a query written inline in `main`, so the test
    suite can ask the same question the script asks rather than a
    re-typed approximation of it.
    """
    from app.models.mailops import EmailOutbox
    return (EmailOutbox.query
            .filter(EmailOutbox.status == 'failed',
                    EmailOutbox.smtp_code.is_(None),
                    EmailOutbox.created_at < cutoff)
            .order_by(EmailOutbox.created_at.asc()).all())


def cancel(rows):
    """Mark them cancelled. One transaction, or none of them."""
    for row in rows:
        row.status = 'cancelled'
        row.last_error = NOTE
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--cutoff', default=CUTOFF.date().isoformat())
    args = ap.parse_args()
    if args.apply == args.check:
        raise SystemExit('Choose one: --check or --apply.')

    cutoff = datetime.fromisoformat(args.cutoff)

    with app.app_context():
        from app.models.mailops import EmailOutbox

        print(f'database: {db.engine.url}')
        print(f'MODE:     {"APPLY" if args.apply else "DRY RUN — nothing is written"}')
        print(f'cutoff:   failed before {cutoff.date()}, with no SMTP '
              f'code\n')

        rows = stale_rows(cutoff)
        kept = (EmailOutbox.query
                .filter(EmailOutbox.status == 'failed')
                .count()) - len(rows)

        print(f'  to cancel                     {len(rows):>5}')
        print(f'  failed rows left alone        {kept:>5}')
        if rows:
            print(f'  oldest  {min(r.created_at for r in rows)}')
            print(f'  newest  {max(r.created_at for r in rows)}')
            print('\n  by the reason recorded:')
            for reason, n in Counter(
                    (r.last_error or '(none)')[:60] for r in rows
            ).most_common(6):
                print(f'      {n:>5}  {reason}')
            print('\n  by event:')
            for ev, n in Counter(r.event_key or '(none)'
                                 for r in rows).most_common(8):
                print(f'      {n:>5}  {ev}')

        if args.check:
            print('\nDRY RUN — nothing written. Re-run with --apply.')
            return

        try:
            cancel(rows)
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'\n  !! ROLLED BACK: {type(exc).__name__}: '
                  f'{str(exc)[:200]}')
            raise SystemExit(1)

        try:
            from app.services import audit
            audit.record('email.stale_cancelled', 'email_outbox', None,
                         new={'count': len(rows),
                              'cutoff': cutoff.date().isoformat()},
                         actor='CRM-TIDY-01', reason=NOTE[:200])
            db.session.commit()
        except Exception:
            db.session.rollback()

        print(f'\n  {len(rows)} row(s) cancelled, in one transaction. '
              f'Not deleted, not resent.')


if __name__ == '__main__':
    main()
