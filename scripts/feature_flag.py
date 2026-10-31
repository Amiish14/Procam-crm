"""
Turn CRM feature flags on and off, with a rollback file.

The TMS has one of these. The CRM had `scripts/set_pilot.py`, which
turns a flag on for a few named people — the right tool for a pilot
and the wrong one for a release, where the question is "everybody,
now" and the thing you want in your hand before you answer it is the
way back.

    list every flag and who it is live for (read-only):
        .venv/bin/python scripts/feature_flag.py

    one flag:
        .venv/bin/python scripts/feature_flag.py --code FEATURE_FF_CRM --on
        .venv/bin/python scripts/feature_flag.py --code FEATURE_FF_CRM --on \\
            --apply

    everything, for everybody:
        .venv/bin/python scripts/feature_flag.py --all-on
        .venv/bin/python scripts/feature_flag.py --all-on --apply

    back to where you were:
        .venv/bin/python scripts/feature_flag.py \\
            --restore backups/flags/flags-20261031-064500.json
        .venv/bin/python scripts/feature_flag.py \\
            --restore backups/flags/flags-20261031-064500.json --apply

The dry run is the default. Nothing is written without `--apply`,
and the dry run prints the same plan the apply will carry out.

For ALL users, not a pilot
    `--all-on` and `--code X --on` both mean on for everybody: the
    flag is switched on *and* both pilot limits are lifted — the
    named-people list and the vertical list. A flag left on with a
    stale pilot list is on for four people and off for the other
    sixty, which is the failure this script exists to stop. Every
    limit it lifts is written into the rollback file first, so a
    restore puts the pilot back exactly as it was.

The kill switch is not a feature
    EMAIL_KILL_SWITCH stops all outbound CRM mail. `--all-on` leaves
    it alone, because "switch everything on" meaning "stop all mail"
    is a sentence nobody intends. `--include-kill-switch` turns it on
    deliberately, and `--code EMAIL_KILL_SWITCH --on` always works.

The rollback file
    Written before the first change, and the run stops if it cannot
    be written. It holds every flag's stored state, both pilot lists,
    and where the current answer came from — database row, `.env`, or
    the flag's own default — so a restore can put a row back, or take
    one away again and let the environment decide as it did before.

    Both ways of being off are kept apart on purpose: a flag with no
    row reads its default, and writing `false` into a row that was
    never there is not a restore, it is a new decision.

Every change goes through `app.services.flags`, so each one leaves
the same audit row the Admin → Switches screen leaves.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

#: Where the rollback files go. `backups/` is outside the repository
#: (it is in .gitignore), so a rollback file written on the server
#: survives a pull and is never committed.
ROLLBACK_DIR = os.path.join('backups', 'flags')

#: The one flag `--all-on` will not touch. See the module docstring.
KILL_SWITCH = 'EMAIL_KILL_SWITCH'


# ── reading where things stand ───────────────────────────────────────
def snapshot():
    """Every flag's stored state and both pilot limits, right now."""
    from app.services import flags

    flags.cache_clear()
    stored = flags._stored()
    rows = []
    for name, description, default in flags.FLAGS:
        rows.append({
            'name': name,
            'description': description,
            'default': bool(default),
            # None means "no row": the environment or the default is
            # answering, and a restore must take the row away again.
            'stored': stored.get(name),
            'live': flags.on(name),
            'source': flags.source_of(name),
            'only_people': list(flags.only_for(name)),
            'only_verticals': list(flags.only_verticals(name)),
        })
    return rows


def resolve(code):
    """The flag this name means, or None.

    Case does not matter and the FEATURE_ prefix is optional, because
    the name in somebody's hand came out of a release note.
    """
    from app.services import flags

    want = (code or '').strip().upper().replace('-', '_')
    if want in flags.DEFAULTS:
        return want
    if ('FEATURE_' + want) in flags.DEFAULTS:
        return 'FEATURE_' + want
    return None


def live_for(name):
    """Active employees this flag is on for, as readable lines."""
    from app import Employee
    from app.services import flags

    out = []
    try:
        people = Employee.query.filter_by(is_active=True).all()
    except Exception:                                     # noqa: BLE001
        return out
    for emp in people:
        if flags.on_for(name, emp.emp_code):
            out.append(f'{emp.emp_code} {emp.name or ""} '
                       f'({emp.vertical or "no vertical"})'.strip())
    return sorted(out)


# ── what a run would do ──────────────────────────────────────────────
def plan_on(names, *, rows=None):
    """One step per flag that is not already on for everybody."""
    rows = rows if rows is not None else snapshot()
    by_name = {r['name']: r for r in rows}
    steps = []
    for name in names:
        row = by_name.get(name)
        if row is None:
            continue
        step = {'name': name, 'set': None, 'lift_people': [],
                'lift_verticals': []}
        if not row['live']:
            step['set'] = True
        elif row['stored'] is not True:
            # On because the environment or the default says so. A row
            # saying True pins it, so a later .env edit cannot take the
            # release back out from under everybody.
            step['set'] = True
        if row['only_people']:
            step['lift_people'] = list(row['only_people'])
        if row['only_verticals']:
            step['lift_verticals'] = list(row['only_verticals'])
        if step['set'] is not None or step['lift_people'] \
                or step['lift_verticals']:
            steps.append(step)
    return steps


def plan_off(names, *, rows=None):
    rows = rows if rows is not None else snapshot()
    by_name = {r['name']: r for r in rows}
    steps = []
    for name in names:
        row = by_name.get(name)
        if row is None:
            continue
        if row['live'] or row['stored'] is not False:
            steps.append({'name': name, 'set': False,
                          'lift_people': [], 'lift_verticals': []})
    return steps


def plan_restore(saved, *, rows=None):
    """What it takes to put a saved file's state back."""
    rows = rows if rows is not None else snapshot()
    by_name = {r['name']: r for r in rows}
    steps = []
    for row in saved.get('flags', []):
        now = by_name.get(row['name'])
        if now is None:
            continue
        step = {'name': row['name'], 'set': None, 'clear': False,
                'people': None, 'verticals': None}
        if row.get('stored') is None:
            # There was no row. Deleting ours is the only honest
            # restore: it puts the environment and the default back in
            # charge, which is where the answer came from before.
            if now['stored'] is not None:
                step['clear'] = True
        elif bool(row['stored']) != now['stored']:
            step['set'] = bool(row['stored'])
        if list(row.get('only_people') or []) != now['only_people']:
            step['people'] = list(row.get('only_people') or [])
        if list(row.get('only_verticals') or []) != now['only_verticals']:
            step['verticals'] = list(row.get('only_verticals') or [])
        if step['set'] is not None or step['clear'] \
                or step['people'] is not None \
                or step['verticals'] is not None:
            steps.append(step)
    return steps


# ── writing ──────────────────────────────────────────────────────────
def _utcnow():
    """Naive UTC, the way the rest of the CRM stores time — taken
    through `timezone.utc` so running a script does not print a
    deprecation warning above the plan it is about to carry out."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def rollback_path(*, now=None, directory=None):
    now = now or _utcnow()
    directory = directory or os.path.join(_ROOT, ROLLBACK_DIR)
    return os.path.join(directory,
                        'flags-' + now.strftime('%Y%m%d-%H%M%S') + '.json')


def write_rollback(path, rows, *, actor='', what=''):
    """The way back, on disk, before anything changes."""
    from app import db

    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = {
        'taken_at': _utcnow().isoformat(timespec='seconds') + 'Z',
        'actor': actor,
        'what': what,
        'database': str(db.engine.url),
        'flags': rows,
    }
    with open(path, 'w') as fh:
        json.dump(body, fh, indent=2, sort_keys=True)
        fh.write('\n')
    return path


def apply_steps(steps, *, actor):
    """Carry out a plan from plan_on / plan_off."""
    from app.services import flags

    done = []
    for step in steps:
        name = step['name']
        if step.get('set') is not None:
            flags.set(name, step['set'], actor=actor)
            done.append((name, 'on' if step['set'] else 'off'))
        if step.get('lift_people'):
            flags.limit_to(name, (), actor=actor)
            done.append((name, 'people limit lifted'))
        if step.get('lift_verticals'):
            flags.limit_verticals(name, (), actor=actor)
            done.append((name, 'vertical limit lifted'))
    flags.cache_clear()
    return done


def apply_restore(steps, *, actor):
    from app.services import flags

    done = []
    for step in steps:
        name = step['name']
        if step.get('clear'):
            flags.clear(name, actor=actor)
            done.append((name, 'row removed — the environment decides'))
        elif step.get('set') is not None:
            flags.set(name, step['set'], actor=actor)
            done.append((name, 'on' if step['set'] else 'off'))
        if step.get('people') is not None:
            flags.limit_to(name, step['people'], actor=actor)
            done.append((name, 'people: '
                         + (', '.join(step['people']) or '(nobody named)')))
        if step.get('verticals') is not None:
            flags.limit_verticals(name, step['verticals'], actor=actor)
            done.append((name, 'verticals: '
                         + (', '.join(step['verticals']) or '(none)')))
    flags.cache_clear()
    return done


# ── printing ─────────────────────────────────────────────────────────
def _rule(n=72):
    print('─' * n)


def listing(rows=None, *, people=False):
    rows = rows if rows is not None else snapshot()
    print(f'{"flag":<34}{"state":<7}{"source":<13}limited to')
    _rule()
    for row in rows:
        limits = []
        if row['only_people']:
            limits.append(', '.join(row['only_people']))
        if row['only_verticals']:
            limits.append('vertical: ' + ', '.join(row['only_verticals']))
        print(f'{row["name"]:<34}'
              f'{("on" if row["live"] else "off"):<7}'
              f'{row["source"]:<13}'
              f'{" · ".join(limits) or "everyone"}')
        if people and row['live']:
            for line in live_for(row['name']):
                print(f'      {line}')
    print()
    on_count = len([r for r in rows if r['live']])
    print(f'{on_count} of {len(rows)} on. '
          f'{len([r for r in rows if r["only_people"] or r["only_verticals"]])}'
          f' limited to a pilot.')


def show_plan(steps, *, restore=False):
    if not steps:
        print('  nothing to do — it is already in that state.')
        return
    for step in steps:
        name = step['name']
        if restore:
            if step.get('clear'):
                print(f'  {name:<34} remove the row (back to the '
                      f'environment)')
            elif step.get('set') is not None:
                print(f'  {name:<34} '
                      f'{"on" if step["set"] else "off"}')
            if step.get('people') is not None:
                print(f'  {name:<34} people → '
                      f'{", ".join(step["people"]) or "(nobody named)"}')
            if step.get('verticals') is not None:
                print(f'  {name:<34} verticals → '
                      f'{", ".join(step["verticals"]) or "(none)"}')
            continue
        bits = []
        if step.get('set') is not None:
            bits.append('on' if step['set'] else 'off')
        if step.get('lift_people'):
            bits.append('lift the people limit ('
                        + ', '.join(step['lift_people']) + ')')
        if step.get('lift_verticals'):
            bits.append('lift the vertical limit ('
                        + ', '.join(step['lift_verticals']) + ')')
        print(f'  {name:<34} {"; ".join(bits)}')


def _shown(path):
    """The path as somebody would type it: relative to the
    repository when it is inside it, and itself when it is not."""
    rel = os.path.relpath(path, _ROOT)
    return path if rel.startswith('..') else rel


def restore_line(path):
    """The command to type to undo what this run did."""
    return ('.venv/bin/python scripts/feature_flag.py --restore '
            + _shown(path) + ' --apply')


# ── the run ──────────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Turn CRM feature flags on and off, with a '
                    'rollback file.')
    ap.add_argument('--all-on', action='store_true',
                    help='every flag on, for ALL users — not a pilot. '
                         'Lifts the people and vertical limits too.')
    ap.add_argument('--code', default='',
                    help='one flag, with or without the FEATURE_ prefix')
    ap.add_argument('--on', action='store_true',
                    help='with --code: on for everybody, limits lifted')
    ap.add_argument('--off', action='store_true',
                    help='with --code: off for everybody')
    ap.add_argument('--restore', default='',
                    help='a rollback file to put back')
    ap.add_argument('--include-kill-switch', action='store_true',
                    help='let --all-on touch EMAIL_KILL_SWITCH, which '
                         'stops all outbound mail')
    ap.add_argument('--people', action='store_true',
                    help='in the listing, name everyone each live flag '
                         'is on for')
    ap.add_argument('--rollback-dir', default='',
                    help=f'where to write the rollback file '
                         f'(default {ROLLBACK_DIR}/)')
    ap.add_argument('--actor', default='DIR12010',
                    help='who is making the change, for the audit row')
    ap.add_argument('--apply', action='store_true',
                    help='write the change. Without it this is a dry run.')
    args = ap.parse_args(argv)

    chosen = [bool(args.all_on), bool(args.code), bool(args.restore)]
    if sum(chosen) > 1:
        raise SystemExit('choose one of --all-on, --code, --restore')
    if args.code and not (args.on or args.off):
        raise SystemExit('--code needs --on or --off')
    if args.code and args.on and args.off:
        raise SystemExit('--on and --off together')

    from app import app as flask_app, db

    with flask_app.app_context():
        from app.services import flags

        print()
        print(f'database: {db.engine.url}')
        print()

        rows = snapshot()

        # ── just looking ────────────────────────────────────────────
        if not any(chosen):
            listing(rows, people=args.people)
            print()
            print('  --code <FLAG> --on | --off     one flag')
            print('  --all-on                       every flag, all users')
            print('  add --apply to write. Without it, nothing changes.')
            print()
            return 0

        # ── what would change ───────────────────────────────────────
        saved = None
        if args.restore:
            path = args.restore
            if not os.path.isabs(path):
                path = os.path.join(_ROOT, path)
            if not os.path.exists(path):
                raise SystemExit(f'no such rollback file: {args.restore}')
            with open(path) as fh:
                saved = json.load(fh)
            what = f'restore {os.path.basename(path)}'
            print(f'Restoring {_shown(path)}')
            print(f'  taken at {saved.get("taken_at", "?")} by '
                  f'{saved.get("actor") or "?"}'
                  + (f' — {saved["what"]}' if saved.get('what') else ''))
            if saved.get('database') and saved['database'] != str(
                    db.engine.url):
                print(f'  ! it was taken against {saved["database"]}')
            steps = plan_restore(saved, rows=rows)
        elif args.all_on:
            names = [r['name'] for r in rows]
            if not args.include_kill_switch:
                names = [n for n in names if n != KILL_SWITCH]
                print(f'{KILL_SWITCH} is left alone — it stops all '
                      f'outbound mail.')
                print('  --include-kill-switch turns it on deliberately.')
                print()
            what = 'all-on'
            steps = plan_on(names, rows=rows)
        else:
            name = resolve(args.code)
            if name is None:
                print('unknown flag. The flags are:')
                for row in rows:
                    print(f'  {row["name"]}')
                raise SystemExit(f'unknown flag {args.code!r}')
            what = f'{name} {"on" if args.on else "off"}'
            steps = (plan_on([name], rows=rows) if args.on
                     else plan_off([name], rows=rows))

        print(('Would change' if not args.apply else 'Changing')
              + f' {len(steps)} flag(s):')
        _rule()
        show_plan(steps, restore=bool(args.restore))
        print()

        # ── the rollback file, before anything moves ────────────────
        directory = (os.path.join(_ROOT, args.rollback_dir)
                     if args.rollback_dir else None)
        path = rollback_path(directory=directory)
        if not args.apply:
            print('Dry run — nothing written.')
            print(f'  the rollback file would be {_shown(path)}')
            print(f'  and the way back would be:\n')
            print(f'    {restore_line(path)}')
            print()
            print('Add --apply to carry this out.')
            print()
            return 0

        if not steps:
            print('Nothing to do, so no rollback file was written.')
            print()
            return 0

        write_rollback(path, rows, actor=args.actor, what=what)
        print(f'Rollback file written first: {_shown(path)}')
        print()

        done = (apply_restore(steps, actor=args.actor) if args.restore
                else apply_steps(steps, actor=args.actor))
        print(f'{len(done)} change(s):')
        _rule()
        for name, detail in done:
            print(f'  {name:<34} {detail}')
        print()

        flags.cache_clear()
        listing(people=args.people)
        print()
        print('The way back, whenever you want it:')
        print(f'\n    {restore_line(path)}\n')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
