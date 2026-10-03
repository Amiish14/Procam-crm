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
    ('weekly_pack', 'weekly_pack'),
    ('freeze', 'freeze'),
)

#: Reports addressed to one person's own board.
PER_PERSON = ('daily', 'exceptions', 'weekly_user')

#: Reports built by app/services/weekly_pack.py rather than digests.
#: Kept as a separate list so `digests.REPORTS` stays the description of
#: the reports that existed before this release.
PACK_REPORTS = ('weekly_pack', 'freeze')

#: Which preference switch silences which report. A person who has
#: turned the daily brief off still gets an escalation; turning off a
#: report turns off that report.
PREF_FOR = {
    'daily': 'daily_brief',
    'exceptions': 'daily_brief',
    'weekly_user': 'weekly_pack',
    'weekly_head': 'weekly_pack',
    'weekly_pack': 'weekly_pack',
}


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
    elif key in ('weekly_head', 'weekly_pack'):
        # The pack goes to the people who will be in the review: the
        # vertical heads, plus whoever the matrix calls management.
        people = vertical_heads()
        if key == 'weekly_pack':
            people = list(dict.fromkeys(people + list(nrules.management())))
    elif key == 'freeze':
        people = list(nrules.management()) + vertical_heads()
        people = list(dict.fromkeys(people))
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
    from app.services import digests, notify_prefs

    row = {'to': emp_code, 'status': '', 'detail': '', 'items': 0}

    pref_key = PREF_FOR.get(key)
    if pref_key:
        prefs = notify_prefs.for_user(emp_code)
        if not prefs.get('email_enabled') or not prefs.get(pref_key, True):
            row['status'] = 'skipped'
            row['detail'] = f'turned off in their preferences ({pref_key})'
            return row

    if key in PACK_REPORTS:
        from app.services import weekly_pack as pack
        if key == 'freeze':
            # Built once: the freeze is one set of numbers, and sending
            # two people different ones would defeat the point of it.
            report = (cache or {}).get('freeze') or pack.freeze_report()
            if cache is not None:
                cache['freeze'] = report
        else:
            report = pack.review_pack(emp_code)
    elif key == 'monthly' and cache is not None and key in cache:
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

    subject = (_pack_subject(report) if key in PACK_REPORTS
               else digests.subject_for(report))
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

    from app.services import flags
    if flags.on('FEATURE_EMAIL_NOTIFY'):
        # Queued rather than sent: the timer that runs this should
        # finish in seconds whether the mail server is healthy or not,
        # and a report that fails should retry rather than vanish.
        # `urgent` because the timer already chose the hour — holding a
        # 08:30 brief for a batching window would deliver it at 08:45.
        from app.services import outbox
        queued = outbox.enqueue(
            to=address, subject=subject, html=html,
            dedupe_key=f'report:{key}:{emp_code}:{report.get("date", "")}',
            event_key=f'report.{key}', user_code=emp_code, urgent=True)
        row['status'] = 'queued' if queued else 'skipped'
        if not queued:
            row['detail'] = 'already queued for today, or not internal'
        return row

    from email_ingest import notifier
    sent = notifier.send(address, subject, html)
    row['status'] = 'sent' if sent else 'failed'
    if not sent:
        row['detail'] = 'the mail server refused it'
    return row


def _pack_subject(report):
    if report['report'] == 'weekly_freeze':
        return f'Week {report["week"]} is frozen — the review numbers'
    return f'Review pack — week {report["week"]}'


def run(key, *, only=None, dry_run=False, send_empty=False, quiet=False):
    enabled = _notify_enabled()
    if key in PACK_REPORTS:
        # The daily and weekly reports already existed and are not
        # flagged — turning a flag on must add behaviour, never take
        # away a report people already rely on. The pack and the freeze
        # are new, so they are off until somebody says otherwise.
        from app.services import flags
        if not flags.on('FEATURE_WEEKLY_PACK'):
            print(f'{key}: FEATURE_WEEKLY_PACK is off — nothing sent.')
            return 0
    if key == 'freeze' and not dry_run:
        # Take the snapshot before anyone is sent it — the email reads
        # the frozen rows, so the order matters.
        from app.services import leases, weekly_pack as pack
        with app.app_context():
            with leases.hold(pack.LEASE_FREEZE, seconds=600) as got:
                if not got:
                    print('freeze: another run holds the lease — skipping.')
                    return 0
                written = pack.freeze()
                print(f'freeze: week {written["week"]} written, '
                      f'{len(written["rows"])} scope(s).')
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
    ap.add_argument('--weekly-pack', dest='weekly_pack', action='store_true',
                    help='the Thursday review pack, before the meeting')
    ap.add_argument('--freeze', action='store_true',
                    help='the Friday freeze: write the week down and send it')
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
