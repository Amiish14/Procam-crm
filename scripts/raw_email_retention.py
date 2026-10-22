"""
The raw .eml is kept for a while, not for ever.

    .venv/bin/python scripts/raw_email_retention.py --check
    .venv/bin/python scripts/raw_email_retention.py --apply
    .venv/bin/python scripts/raw_email_retention.py --set-days 180

A forwarded enquiry carries its own history in the headers — who
sent it first, who passed it on, when. The CRM parses what it
needs and the rest is only in the file, which is why the file is
kept at all: when the SuperProcure postings stopped, nobody could
say who had been forwarding them, and no .eml survived to answer.

180 days by default, set in `admin_settings` under
`raw_email_retention_days` so an administrator can change it
without a release. Past that the file goes and the row stays —
the row says a message existed, which is cheap and is the part
somebody looks for; the file is the bulky part and the part with
a customer's whole message in it.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import app, db                                   # noqa: E402

SETTING = 'raw_email_retention_days'
DEFAULT_DAYS = 180
PURGED = 'purged'


def days():
    from app.services import admin_settings
    try:
        value = int(admin_settings.get(SETTING) or DEFAULT_DAYS)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return max(7, value)


def due(cutoff):
    from app.models.mailops import LeadRawEmail

    return (LeadRawEmail.query
            .filter(LeadRawEmail.received_at < cutoff,
                    LeadRawEmail.status != PURGED,
                    LeadRawEmail.storage_path.isnot(None))
            .order_by(LeadRawEmail.received_at.asc()).all())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--set-days', dest='set_days', type=int,
                    default=None)
    ap.add_argument('--actor', default='system')
    args = ap.parse_args()

    with app.app_context():
        from app.services import admin_settings
        print(f'database: {db.engine.url}')

        if args.set_days is not None:
            if args.set_days < 7:
                raise SystemExit('A week is the floor — below that '
                                 'the file is gone before anybody '
                                 'notices they need it.')
            admin_settings.save(SETTING, str(args.set_days),
                                actor=args.actor)
            print(f'  retention set to {args.set_days} days.')
            return

        keep = days()
        cutoff = datetime.utcnow() - timedelta(days=keep)
        rows = due(cutoff)
        print(f'MODE:     {"APPLY" if args.apply else "DRY RUN"}\n')
        print(f'  retention                {keep} days '
              f'(admin_settings.{SETTING})')
        print(f'  cutoff                   {cutoff:%Y-%m-%d}')
        print(f'  files past it            {len(rows)}')
        if rows:
            print(f'  oldest                   '
                  f'{rows[0].received_at:%Y-%m-%d}')

        if not args.apply:
            print('\nDRY RUN — nothing deleted.')
            return

        gone = 0
        for row in rows:
            path = row.storage_path
            try:
                if path and os.path.isfile(path):
                    os.remove(path)
                    gone += 1
            except OSError as exc:
                print(f'  !! {path}: {exc}')
                continue
            # The row stays. It says a message existed, which is
            # the part somebody looks for later; the file is the
            # bulky part and the one with a customer's whole
            # message in it.
            row.status = PURGED
            row.storage_path = None
        db.session.commit()
        print(f'\n  {gone} file(s) removed, {len(rows)} row(s) '
              f'marked {PURGED}. The rows stay.')


if __name__ == '__main__':
    main()
