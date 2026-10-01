"""
Send the scheduled CRM reports.

    python scripts/send_reports.py --daily
    python scripts/send_reports.py --exceptions
    python scripts/send_reports.py --weekly-user
    python scripts/send_reports.py --weekly-head
    python scripts/send_reports.py --monthly

    python scripts/send_reports.py --daily --dry-run      # print, send nothing
    python scripts/send_reports.py --daily --to EMP001    # one person

Content comes from `app/services/digests.py`, which reads each person's
own Workbench board, so a report can never show somebody a record they
are not allowed to open. Delivery is `app/services/notify.py` and the
existing Graph transport; nothing here talks to a mail server directly.

One bad recipient must not cost everybody else their report, so every
person is built and sent inside their own try/except and the run ends
with a line per recipient. Exit 0 when everyone was served, 1 when at
least one person failed, 2 for a bad invocation.

Who gets what
    daily, exceptions, weekly-user  everyone active who owns work: a
                                    lead, as primary or secondary, or an
                                    account. Nobody is mailed a report
                                    about an empty board.
    weekly-head                     everyone marked as a vertical head.
    monthly                         the administrators, or the codes in
                                    REPORT_MANAGEMENT_CODES.

Optional environment (nothing here is required)
    NOTIFY_ENABLED            'false' stops all outbound mail — the run
                              still reports what it would have sent.
    CRM_BASE_URL              absolute root for the links in the email.
                              Without it the buttons are relative and
                              will not work from a mail client.
    REPORT_MANAGEMENT_CODES   comma-separated employee codes for the
                              monthly report, instead of administrators.
    REPORT_MAX_RECIPIENTS     a ceiling for one run, as a guard while
                              the reports are being introduced.

Run by the procam-crm-{daily,exceptions,weekly,monthly}-report timers in
docs/operations/deploy/.
"""
import argparse
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import app, db                                        # noqa: E402

#: CLI flag → the digest key in app/services/digests.REPORTS.
REPORT_FLAGS = (
    ('daily', 'daily'),
    ('exceptions', 'exceptions'),
    ('weekly_user', 'weekly_user'),
    ('weekly_head', 'weekly_head'),
    ('monthly', 'monthly'),
)

#: Reports addressed to one person's own board.
PER_PERSON = ('daily', 'exceptions', 'weekly_user')


def _notify_enabled():
    from email_ingest import notifier
    return notifier.is_enabled()


def working_people():
    """Active employees who own something worth reporting on.

    Everyone with a lead (as primary or secondary PIC) or an account.
    Mailing the whole employee master would send a daily empty report
    to finance and IT, and an unread report trains people to filter the
    sender.
    """
    from app import Employee, Lead, Company
    codes = set()
    for col in (Lead.assigned_to, Lead.secondary_owner,
                Company.pic_emp_code, Company.secondary_pic_emp_code):
        try:
            codes.update(c for (c,) in db.session.query(col).distinct() if c)
        except Exception:
            # A secondary-PIC column arrives with its migration. On an
            # instance where that has not been applied, the people with
            # a primary record should still get their report rather
            # than the whole run stopping.
            db.session.rollback()
    rows = (Employee.query
            .filter(Employee.is_active.is_(True))
            .filter(Employee.emp_code.in_(codes or {''}))
            .order_by(Employee.emp_code.asc()).all())
    return [e.emp_code for e in rows if e.emp_code and (e.email or '').strip()]


def vertical_heads():
    from app import Employee
    rows = (Employee.query
            .filter(Employee.is_active.is_(True),
                    Employee.is_vertical_head.is_(True))
            .order_by(Employee.emp_code.asc()).all())
    return [e.emp_code for e in rows if e.emp_code and (e.email or '').strip()]


def recipients(key, only=None):
    """Who this run is for."""
    from app.services import notification_rules as nrules
    if only:
        return [only.strip().upper()]
    if key in PER_PERSON:
        people = working_people()
    elif key == 'weekly_head':
        people = vertical_heads()
    else:
        people = nrules.management()
    cap = (os.environ.get('REPORT_MAX_RECIPIENTS') or '').strip()
    if cap.isdigit():
        people = people[:int(cap)]
    return people


def _address(emp_code):
    from app import Employee
    emp = Employee.query.filter_by(emp_code=emp_code).first()
    return (getattr(emp, 'email', '') or '').strip()


def _one(key, emp_code, *, dry_run, send_empty, enabled, cache=None):
    """Build and send one person's report. Returns a summary row.

    `cache` holds the company-wide report so the monthly run reads the
    whole database once rather than once per recipient — and so every
    recipient is sent the same figures.
    """
    from app.services import digests

    row = {'to': emp_code, 'status': '', 'detail': '', 'items': 0}
    if key == 'monthly' and cache is not None and key in cache:
        report = cache[key]
    else:
        report = digests.build(key, emp_code if key != 'monthly' else None)
        if key == 'monthly' and cache is not None:
            cache[key] = report
    row['items'] = report.get('total', 0)
    if report.get('empty') and not send_empty:
        row['status'] = 'skipped'
        row['detail'] = 'nothing to report'
        return row

    subject = digests.subject_for(report)
    html = digests.render(report)
    row['detail'] = subject

    if dry_run:
        row['status'] = 'dry-run'
        return row
    if not enabled:
        row['status'] = 'disabled'
        row['detail'] = 'NOTIFY_ENABLED is off — ' + subject
        return row

    address = _address(emp_code)
    if not address:
        row['status'] = 'failed'
        row['detail'] = 'no email address on file'
        return row

    from email_ingest import notifier
    sent = notifier.send(address, subject, html)
    row['status'] = 'sent' if sent else 'failed'
    if not sent:
        row['detail'] = 'the mail server refused it'
    return row


def run(key, *, only=None, dry_run=False, send_empty=False, quiet=False):
    enabled = _notify_enabled()
    rows, failures, cache = [], 0, {}
    with app.app_context():
        try:
            people = recipients(key, only=only)
        except Exception as exc:                              # noqa: BLE001
            # Nobody was served, so this is not a partial failure: say
            # so loudly and exit 2 rather than reporting "0 sent".
            db.session.rollback()
            print(f'{key}: could not work out who to send to — {exc}',
                  file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return 2
        if not people:
            print(f'{key}: nobody to send to.')
            return 0
        for emp_code in people:
            try:
                rows.append(_one(key, emp_code, dry_run=dry_run,
                                 send_empty=send_empty, enabled=enabled,
                                 cache=cache))
            except Exception as exc:                          # noqa: BLE001
                # One person's report failing is one person's problem.
                # The traceback goes to the journal; the run continues.
                db.session.rollback()
                rows.append({'to': emp_code, 'status': 'failed',
                             'items': 0, 'detail': str(exc)[:160]})
                traceback.print_exc(file=sys.stderr)

    for row in rows:
        if row['status'] == 'failed':
            failures += 1
        if not quiet:
            print(f'  {row["to"]:<10} {row["status"]:<9} '
                  f'{row["items"]:>4} item(s)  {row["detail"][:70]}')
    counts = {}
    for row in rows:
        counts[row['status']] = counts.get(row['status'], 0) + 1
    print(f'{key}: ' + ', '.join(f'{n} {s}' for s, n in sorted(counts.items()))
          + f' (of {len(rows)} recipient(s))'
          + ('' if enabled else ' — NOTIFY_ENABLED is off'))
    return 1 if failures else 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Send the scheduled Procam CRM reports.')
    ap.add_argument('--daily', action='store_true',
                    help="the morning action list")
    ap.add_argument('--exceptions', action='store_true',
                    help='the end-of-day list of what is still open')
    ap.add_argument('--weekly-user', dest='weekly_user', action='store_true',
                    help="one person's week, against the week before")
    ap.add_argument('--weekly-head', dest='weekly_head', action='store_true',
                    help="a vertical head's team review")
    ap.add_argument('--monthly', action='store_true',
                    help='the company-wide management report')
    ap.add_argument('--dry-run', dest='dry_run', action='store_true',
                    help='build and print, send nothing')
    ap.add_argument('--to', metavar='CODE',
                    help='one employee code, for testing')
    ap.add_argument('--send-empty', dest='send_empty', action='store_true',
                    help='send a report even when there is nothing in it')
    ap.add_argument('--quiet', action='store_true',
                    help='the totals only, no per-recipient lines')
    args = ap.parse_args(argv)

    chosen = [key for attr, key in REPORT_FLAGS if getattr(args, attr)]
    if not chosen:
        ap.error('choose a report: --daily, --exceptions, --weekly-user, '
                 '--weekly-head or --monthly')
    if len(chosen) > 1:
        ap.error('one report at a time, so a failure is attributable')

    return run(chosen[0], only=args.to, dry_run=args.dry_run,
               send_empty=args.send_empty, quiet=args.quiet)


if __name__ == '__main__':
    raise SystemExit(main())
