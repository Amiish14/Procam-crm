"""
Requeue the two notifications Exchange refused on 16 October.

    .venv/bin/python scripts/resend_554.py --check
    .venv/bin/python scripts/resend_554.py --apply

#5357 and #5358, both about lead 12208 (Alstom): a forwarded RFP
captured as a 15.1 MB `.eml` plus the ten documents taken out of it.
Both came back

    554 5.6.0 Invalid message content when handling MIME exception

because `message/rfc822` was base64-encoded, which RFC 2046 §5.2.1
forbids. They failed at 04:31 on the 16th; `_attach_message` fixed it
at 18:24 on the 17th. They are not evidence of a hole in the fix —
they are a day older than it.

Rebuilt today, the message carries all ten documents, no rfc822 part
at all (the 15.1 MB original sorts last and falls outside the 15 MB
budget), and every part legally encoded.

Requeued only while they are still worth sending: under seven days
old and the lead still open. Otherwise cancelled, because a
notification about a lead somebody has since closed is noise with a
date on it.

Setting `status='queued'` is the whole resend: attachments are
resolved at send time, so the worker rebuilds the message with
today's code rather than replaying the bytes that failed.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db, STAGES_TERMINAL            # noqa: E402

TASK_IDS = (5357, 5358)
MAX_AGE_DAYS = 7


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    if args.apply == args.check:
        raise SystemExit('Choose one: --check or --apply.')

    with app.app_context():
        from app.models.mailops import EmailOutbox
        from app.services import audit

        now = datetime.utcnow()
        print(f'database: {db.engine.url}')
        print(f'MODE:     {"APPLY" if args.apply else "DRY RUN — nothing is written"}\n')

        resend, cancel = [], []
        for tid in TASK_IDS:
            row = db.session.get(EmailOutbox, tid)
            if row is None:
                print(f'  #{tid}: gone'); continue
            age = (now - row.created_at).days if row.created_at else 999
            lead = (db.session.get(Lead, row.entity_id)
                    if row.entity_id else None)
            lead_open = (lead is not None
                         and (lead.stage or '') not in STAGES_TERMINAL
                         and not getattr(lead, 'is_archived', False))
            why = []
            if age >= MAX_AGE_DAYS:
                why.append(f'{age} days old')
            if not lead_open:
                why.append(f'lead {row.entity_id} is '
                           + (lead.stage if lead else 'missing'))
            verdict = 'resend' if not why else 'cancel: ' + ', '.join(why)
            (resend if not why else cancel).append(row)
            print(f'  #{tid} {row.event_key:<26} to {row.to_addr:<32} '
                  f'age {age}d  lead {row.entity_id} '
                  f'{lead.stage if lead else "?"}  -> {verdict}')

        print(f'\n  to resend  {len(resend)}')
        print(f'  to cancel  {len(cancel)}')

        if args.check:
            print('\nDRY RUN — nothing written. Re-run with --apply.')
            return

        try:
            for row in resend:
                row.status = 'queued'
                row.attempts = 0
                row.smtp_code = None
                row.last_error = None
                row.not_before = datetime.utcnow()
                audit.record('email.requeued', 'email_outbox', str(row.id),
                             old={'status': 'failed', 'smtp_code': 554},
                             new={'status': 'queued'},
                             actor='CRM-UAT-CLOSEOUT-01',
                             reason='Refused with 554 the day before the '
                                    'MIME fix; rebuilt with current code.')
            for row in cancel:
                row.status = 'cancelled'
                row.last_error = ('Not resent: too old, or the lead is '
                                  'closed. Originally refused with 554 '
                                  'before the 17 Oct MIME fix.')
                audit.record('email.cancelled', 'email_outbox', str(row.id),
                             old={'status': 'failed'},
                             new={'status': 'cancelled'},
                             actor='CRM-UAT-CLOSEOUT-01',
                             reason=row.last_error[:200])
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'\n  !! ROLLED BACK: {type(exc).__name__}: '
                  f'{str(exc)[:200]}')
            raise SystemExit(1)

        print(f'\n  {len(resend)} requeued, {len(cancel)} cancelled. '
              f'The worker rebuilds the message at send time, so these '
              f'go out with today\'s code, not the bytes that failed.')


if __name__ == '__main__':
    main()
