"""
Who holds each step, per vertical. Names resolved, never guessed.

    .venv/bin/python scripts/set_rfq_chain_people.py --check
    .venv/bin/python scripts/set_rfq_chain_people.py --apply

§4.1 says who is responsible comes from a small Admin table and
not from code, so these are `app_settings` rows:

    rfq_coordinator        one employee code
    rfq_scm_by_vertical    {vertical: emp_code}
    rfq_ops_by_vertical    {vertical: emp_code}

Every name is looked up and every code is checked against the
name beside it. A name that matches two people, or a code whose
name disagrees, stops the run rather than being resolved by
guesswork — the whole point of this table is that step 3 lands on
a real person, and a plausible wrong answer here is worse than an
empty cell, which at least shows up as an unowned step.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Employee, app, db                         # noqa: E402

#: EMP1552018 was given first and is Abhishek Singh,
#: inactive. The script refused it rather than
#: resolving the name over the code, which is why this
#: line is a correction and not a silent fix.
COORDINATOR = ('Seema Chattopadhyay', 'EMP2882022')

SCM = {
    'Heavy Transport':    ('Suranjan Aon', None),
    'Project Freight':    ('Sanjeev Paliwal', None),
    'Freight Forwarding': ('Sanjeev Paliwal', None),
    'Installation':       ('Suranjan Aon', None),
    'Warehousing':        ('Pravin Choudhary', 'EMP4092026'),
}
OPS = {
    'Heavy Transport':    ('Dipanka Talukder', None),
    'Project Freight':    ('Sanjeev Paliwal', None),
    'Freight Forwarding': ('Sanjeev Paliwal', None),
    'Installation':       ('Dipanka Talukder', None),
    'Warehousing':        ('Vipulsinh Zala', None),
}


def resolve(name, code=None):
    """(emp_code, note) or (None, why). Active people only."""
    wanted = ' '.join((name or '').split()).lower()
    active = Employee.query.filter_by(is_active=True).all()

    if code:
        byc = Employee.query.filter_by(emp_code=code).first()
        if byc is None:
            return None, f'{code} is not an employee code on file'
        if not getattr(byc, 'is_active', True):
            return None, (f'{code} is {byc.name}, who is no longer '
                          f'active')
        if ' '.join((byc.name or '').split()).lower() != wanted:
            return None, (f'{code} is {byc.name}, not {name} — the '
                          f'code and the name disagree')
        return code, ''

    #: Exact first, then every-word, so "Sanjeev Paliwal" finds
    #: "Sanjeev Kumar Paliwal" and "Vipulsinh Zala" finds
    #: "Vipul Sinh Zala" without matching a second person.
    exact = [e for e in active
             if ' '.join((e.name or '').split()).lower() == wanted]
    if len(exact) == 1:
        return exact[0].emp_code, ''

    words = [w for w in wanted.replace('sinh', ' sinh').split() if w]
    loose = [e for e in active
             if all(w in (e.name or '').lower() for w in words)]
    if len(loose) == 1:
        return loose[0].emp_code, f'matched {loose[0].name}'
    if not loose:
        return None, f'no active employee called {name}'
    return None, (f'{name} matches {len(loose)}: '
                  + ', '.join(f'{e.emp_code} {e.name}'
                              for e in loose[:4]))


def build():
    problems, out = [], {}
    code, why = resolve(*COORDINATOR)
    if code:
        out['rfq_coordinator'] = code
    else:
        problems.append(f'rfq_coordinator — {why}')

    for label, table in (('rfq_scm_by_vertical', SCM),
                         ('rfq_ops_by_vertical', OPS)):
        resolved = {}
        for vertical, (name, forced) in table.items():
            code, why = resolve(name, forced)
            if code:
                resolved[vertical] = code
            else:
                problems.append(f'{label}[{vertical}] — {why}')
        out[label] = resolved
    return out, problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--actor', default='DIR12010')
    args = ap.parse_args()
    if args.check == args.apply:
        raise SystemExit('Choose one: --check or --apply.')

    with app.app_context():
        from app.services import admin_settings

        print(f'database: {db.engine.url}')
        print(f'MODE:     {"APPLY" if args.apply else "DRY RUN"}\n')
        settings, problems = build()

        def _name(code):
            emp = Employee.query.filter_by(emp_code=code).first()
            return getattr(emp, 'name', '?') or '?'

        print(f'  rfq_coordinator   '
              f'{settings.get("rfq_coordinator", "— UNRESOLVED —")}'
              f'  {_name(settings.get("rfq_coordinator", ""))}')
        for label in ('rfq_scm_by_vertical', 'rfq_ops_by_vertical'):
            print(f'\n  {label}:')
            for vertical, code in sorted(settings.get(label,
                                                      {}).items()):
                print(f'    {vertical:<22}{code:<13}{_name(code)}')
            missing = [v for v in (SCM if 'scm' in label else OPS)
                       if v not in settings.get(label, {})]
            for vertical in missing:
                print(f'    {vertical:<22}— UNRESOLVED —')

        if problems:
            print('\n  NOT RESOLVED — nothing is guessed here, '
                  'because a plausible wrong name sends real work '
                  'to the wrong desk:')
            for line in problems:
                print(f'    {line}')

        if args.check:
            print('\nCHECK ONLY — nothing written.')
            return

        # A setting is written only when it is complete. A
        # half-filled vertical table silently drops the verticals
        # it could not place, and those steps then open owned by
        # nobody with nothing to say why.
        wanted = {'rfq_scm_by_vertical': SCM,
                  'rfq_ops_by_vertical': OPS}
        written, held = [], []
        for key, value in settings.items():
            table = wanted.get(key)
            if table is not None and len(value) != len(table):
                held.append(f'{key} — only {len(value)} of '
                            f'{len(table)} verticals resolved')
                continue
            if not value:
                held.append(f'{key} — nothing resolved')
                continue
            admin_settings.save(key, value, actor=args.actor)
            written.append(key)
        for line in problems:
            key = line.split(' —')[0].split('[')[0]
            if key not in written and key not in \
                    [h.split(' —')[0] for h in held]:
                held.append(line)
        db.session.commit()

        print(f'\n  {len(written)} setting(s) written: '
              f'{", ".join(written) or "none"}')
        if held:
            print('  not written, and why:')
            for line in held:
                print(f'    {line}')
            print('  Those steps will open owned by nobody until '
                  'the name is settled — visible, which is the '
                  'point.')


if __name__ == '__main__':
    main()
