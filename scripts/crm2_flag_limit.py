"""
Turn a flag on for named people only.

    .venv/bin/python scripts/crm2_flag_limit.py --show
    .venv/bin/python scripts/crm2_flag_limit.py --on --only DIR12010
    .venv/bin/python scripts/crm2_flag_limit.py --lift
    .venv/bin/python scripts/crm2_flag_limit.py --off

`--on --only DIR12010` turns FEATURE_CRM2 on and limits it to that
employee: the cockpit, the search box and the stage map exist for
them and 404 for everybody else, including other administrators.

The limit narrows and never grants, so `--off` is absolute whoever
is listed. `--lift` removes the list and leaves the flag as it is,
which is how a review ends.

Both parts are audited — `flag.changed` and `flag.limited` — because
"who turned CRM 2.0 on" is a question that gets asked.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import app, db                                   # noqa: E402,F401
from app.services import flags                            # noqa: E402

FLAG = 'FEATURE_CRM2'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--flag', default=FLAG)
    ap.add_argument('--show', action='store_true')
    ap.add_argument('--on', action='store_true')
    ap.add_argument('--off', action='store_true')
    ap.add_argument('--lift', action='store_true')
    ap.add_argument('--only', nargs='*', default=None,
                    metavar='EMP_CODE')
    ap.add_argument('--actor', default='DIR12010')
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        name = args.flag
        if name not in flags.NAMES:
            raise SystemExit(f'unknown flag {name!r}; known: '
                             + ', '.join(flags.NAMES))

        def report():
            only = flags.only_for(name)
            print(f'  {name}: '
                  + ('ON' if flags.on(name) else 'off')
                  + f'  (source: {flags.source_of(name)})')
            print('  visible to: '
                  + (', '.join(only) if only else 'everybody'))

        if args.show or not (args.on or args.off or args.lift
                             or args.only is not None):
            report()
            return

        if args.off:
            flags.set(name, False, actor=args.actor)
            print(f'  {name} turned off for everybody.')
        elif args.on:
            flags.set(name, True, actor=args.actor)
            print(f'  {name} turned on.')

        if args.lift:
            flags.limit_to(name, [], actor=args.actor)
            print('  limit lifted — everybody who can sign in sees it.')
        elif args.only is not None:
            codes = flags.limit_to(name, args.only, actor=args.actor)
            print('  limited to: ' + (', '.join(codes) or 'nobody'))

        print()
        report()


if __name__ == '__main__':
    main()
