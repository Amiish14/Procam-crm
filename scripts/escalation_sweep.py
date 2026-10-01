"""
Quote / RFQ escalation sweep — Group C.

Every fifteen minutes, this asks `app.services.escalation` what is due
and has not been sent, and sends it. Everything that decides *what* is
due lives in the service, so what this script prints with --dry-run is
exactly what it would send without it.

    python scripts/escalation_sweep.py --dry-run --verbose
    python scripts/escalation_sweep.py --verbose
    python scripts/escalation_sweep.py --at '2026-10-10 09:00'

Safe to run every fifteen minutes:
  * each (RFQ, level, recipient) is sent once and recorded, so a second
    run in the same quarter-hour sends nothing;
  * one record failing never stops the rest;
  * an escalation whose moment passed more than the configured backlog
    window ago is not sent at all, so the first run after a deployment
    does not mail everybody about every RFQ the CRM has ever held.

Exit status is 0 when the sweep ran, 1 when it could not run at all. A
delivery that failed is reported and retried next time, not an error:
the mail server being down for an hour must not page anybody.
"""
import argparse
import os
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import app                                                # noqa: E402
from app.services import escalation                                # noqa: E402


def _when(text):
    if not text:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise SystemExit(f'Could not read --at "{text}" (use YYYY-MM-DD HH:MM).')


def _line(p):
    return (f'    {p["stage"]:<22} {p["reference"]:<16} → {p["to"]:<8} '
            f'(due {str(p["due_at"])[:16]})')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dry-run', action='store_true',
                    help='print what would be sent and send nothing')
    ap.add_argument('--verbose', '-v', action='store_true')
    ap.add_argument('--at', help='pretend it is this moment (UTC), for '
                                 'checking a timing change before it runs')
    args = ap.parse_args()

    with app.app_context():
        now = _when(args.at) or datetime.utcnow()
        if args.verbose:
            # What is actually in force, which is not always what an
            # administrator believes they set.
            print('escalation levels in force:')
            for s in escalation.ladder():
                state = 'on ' if s['active'] else 'OFF'
                print(f'    [{state}] {s["key"]:<22} '
                      f'{s["hours"]:>6.1f}h from {s["anchor"]:<8} '
                      f'→ {s["to"]:<14} ({s["source"]})')
            print(f'    backlog window: {escalation.max_backlog_hours():g}h')

        if not args.dry_run:
            try:
                # Seeding the configuration is additive and idempotent; a
                # database that has never seen it still escalates, on the
                # documented defaults, and shows an administrator the
                # rows. A dry run writes nothing at all, including this.
                escalation.ensure_table()
                escalation.ensure_rules()
            except Exception as exc:
                print(f'  [WARN] could not seed the escalation '
                      f'configuration: {exc}')

        try:
            report = escalation.run(now=now, dry_run=args.dry_run)
        except Exception as exc:
            print(f'  [FAIL] the sweep could not run: {exc}')
            return 1

        if args.dry_run:
            plans = report['would_send']
            print(f'  == DRY-RUN == would send {len(plans)}; nothing sent.')
            for p in plans if args.verbose else plans[:20]:
                print(_line(p))
            if not args.verbose and len(plans) > 20:
                print(f'    … and {len(plans) - 20} more (--verbose for all)')
            return 0

        print(f'  [OK] due={report["considered"]} sent={len(report["sent"])} '
              f'failed={len(report["failed"])}')
        if args.verbose:
            for p in report['sent']:
                print(_line(p) + ('  [emailed]' if p.get('emailed') else ''))
        for p in report['failed']:
            # Reported, not fatal: it will be retried on the next sweep.
            print(f'  [RETRY LATER] {p["stage"]} {p["reference"]} → '
                  f'{p["to"]}: {p.get("error")}')
        print('Done.')
        return 0


if __name__ == '__main__':
    sys.exit(main())
