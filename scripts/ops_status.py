"""
Operations status. Read-only. For a timer, a cron alert or a person.

    .venv/bin/python scripts/ops_status.py
    .venv/bin/python scripts/ops_status.py --json
    .venv/bin/python scripts/ops_status.py --only health,backups,disk
    .venv/bin/python scripts/ops_status.py --no-network --no-schema
    .venv/bin/python scripts/ops_status.py --write-status instance/ops_status.json

Every check is in app/ops/checks.py; what each means, its thresholds and
what to do about a WARN or FAIL is in docs/operations/MONITORING_GUIDE.md.

Exit status is 1 when any check FAILs and 0 otherwise — WARN and UNKNOWN
are for the admin page, not for waking someone — so a cron line can be

    ops_status.py --no-schema >/dev/null || <send an alert>

--write-status writes the JSON report atomically, for the /admin/ops page:
the web process reads it instead of running slow or privileged checks.

The checks are loaded by path, not imported through the app package:
importing app.py boots the application against the live database, which
a monitor must never do.
"""
import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, '.env'))
except ImportError:                                  # pragma: no cover
    pass


def load_checks():
    name = 'procam_ops_checks'
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_ROOT, 'app', 'ops', 'checks.py'))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def parse_args(argv, keys):
    ap = argparse.ArgumentParser(
        description='Procam CRM operations status (read-only).')
    ap.add_argument('--json', action='store_true',
                    help='print the report as JSON')
    ap.add_argument('--only', default='',
                    help='comma-separated check keys: ' + ', '.join(keys))
    ap.add_argument('--no-network', action='store_true',
                    help='skip Graph and the TLS handshake (the local '
                         '/healthz call still runs)')
    ap.add_argument('--no-schema', action='store_true',
                    help='skip the schema comparison (imports the app in a '
                         'child process against an in-memory database; '
                         'takes several seconds)')
    ap.add_argument('--write-status', metavar='PATH',
                    help='also write the JSON report here, atomically')
    ap.add_argument('--db', help='database file (default: DATABASE_URL)')
    ap.add_argument('--backups-dir', help='default: <repo>/backups')
    args = ap.parse_args(argv)
    only = [k.strip() for k in args.only.split(',') if k.strip()]
    unknown = [k for k in only if k not in keys]
    if unknown:
        ap.error('unknown check(s): ' + ', '.join(unknown))
    args.only = only
    return args


def main(argv=None):
    checks = load_checks()
    args = parse_args(argv, checks.CHECK_KEYS)
    ctx = checks.Context(db_path=args.db, backups_dir=args.backups_dir,
                         network=not args.no_network,
                         schema=not args.no_schema)
    results = checks.run_checks(ctx, only=args.only or None)
    report = checks.build_report(results, ctx, only=args.only or None)
    if args.write_status:
        checks.write_status(report, args.write_status)
    if args.json:
        print(json.dumps(report, indent=1, default=str))
    else:
        print(f'\n  Procam CRM operations status — '
              f'{datetime.now():%Y-%m-%d %H:%M} on {report["host"]}\n')
        for r in results:
            print(f'    {r["status"]:<7} {r["label"]:<30} {r["detail"]}')
        c = report['counts']
        print(f'\n  {c["OK"]} ok · {c["WARN"]} warn · {c["FAIL"]} fail · '
              f'{c["UNKNOWN"]} unknown'
              + (f'   (written to {args.write_status})'
                 if args.write_status else ''))
    return checks.exit_code(results)


if __name__ == '__main__':
    raise SystemExit(main())
