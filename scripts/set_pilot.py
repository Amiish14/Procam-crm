"""
Turn a flag on for a few people and one vertical.

    .venv/bin/python scripts/set_pilot.py --flag FEATURE_ACCOUNT_FIRST \
        --people DIR12010 --verticals Warehousing --apply
    .venv/bin/python scripts/set_pilot.py --flag FEATURE_ACCOUNT_FIRST --check
    .venv/bin/python scripts/set_pilot.py --flag FEATURE_ACCOUNT_FIRST --off

Two limits, and they are an OR: "this person, and everybody in
that vertical". Requiring both would mean naming every member of
the vertical as well — the list that goes stale the first time
somebody joins.

`--check` prints who the flag is live for, by name, and that is
the thing to read before walking away: a pilot nobody can see is
as wrong as one everybody can.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Employee, app, db                         # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--flag', required=True)
    ap.add_argument('--people', default='')
    ap.add_argument('--verticals', default='')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--off', action='store_true',
                    help='turn the flag off entirely and lift both '
                         'limits')
    ap.add_argument('--actor', default='DIR12010')
    args = ap.parse_args()

    with app.app_context():
        from app.services import flags

        name = args.flag
        if name not in flags.DEFAULTS:
            raise SystemExit(f'unknown flag {name!r}')
        print(f'database: {db.engine.url}')

        if args.off:
            # `flags.set_flag` has never existed — the function is
            # `flags.set` — and the hasattr guard meant --off lifted
            # both limits and left the flag ON for everybody, which is
            # the opposite of what somebody reaching for --off wants.
            flags.set(name, False, actor=args.actor)
            flags.limit_to(name, (), actor=args.actor)
            flags.limit_verticals(name, (), actor=args.actor)
            print(f'  {name}: off, limits lifted')
        elif args.apply:
            people = [p for p in args.people.split(',') if p.strip()]
            verts = [v for v in args.verticals.split(',') if v.strip()]
            flags.limit_to(name, people, actor=args.actor)
            flags.limit_verticals(name, verts, actor=args.actor)
            _turn_on(name, actor=args.actor)
            print(f'  {name}: on, limited')

        flags.cache_clear()
        print(f'\n  flag on            {flags.on(name)}')
        print(f'  limited to people  '
              f'{", ".join(flags.only_for(name)) or "(nobody named)"}')
        print(f'  limited to verticals '
              f'{", ".join(flags.only_verticals(name)) or "(none)"}')

        live = []
        for emp in Employee.query.filter_by(is_active=True).all():
            if flags.on_for(name, emp.emp_code):
                live.append(f'{emp.emp_code} {emp.name} '
                            f'({emp.vertical or "no vertical"})')
        print(f'\n  live for {len(live)} active employee(s):')
        for line in sorted(live):
            print(f'    {line}')
        if not live and flags.on(name):
            print('    nobody — check the codes and vertical names')


def _turn_on(name, *, actor):
    import json

    from app.models.integration import AppSetting

    key = 'flag.' + name
    row = AppSetting.query.filter_by(key=key).first()
    if row is None:
        row = AppSetting(key=key)
        db.session.add(row)
    row.value_json = json.dumps(True)
    row.updated_by = actor
    db.session.commit()


if __name__ == '__main__':
    main()
