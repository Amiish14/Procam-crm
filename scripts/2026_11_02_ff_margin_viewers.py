"""
Who sees Freight Forwarding buy rates and margin — exactly four
people.

    .venv/bin/python scripts/2026_11_02_ff_margin_viewers.py --check
    .venv/bin/python scripts/2026_11_02_ff_margin_viewers.py --apply
    .venv/bin/python scripts/2026_11_02_ff_margin_viewers.py --down --yes

The 20 October decision, confirmed in PROCAM-DECISIONS-02NOV: "FF buy
rates / margin view: Nilesh, TGR, Bhavin and Sanjna only. Grant
FF_JOB.VIEW_BUY_RATES / VIEW_MARGIN to exactly these before FF
features go on."

The code default now names the four, but a default only applies where
nothing is saved, and `margin_viewers_ff` is an Admin screen that has
been saved before. So the live row is set here, and the old value is
printed and audited rather than overwritten quietly.

The CRM half is this setting. The TMS half is the two rights the
decision names, `FF_JOB.VIEW_BUY_RATES` and `FF_JOB.VIEW_MARGIN`,
which live in the other repo — and which the role table grants to
every admin, a larger group than four. That is changed there, not
here; this script prints the reminder so the two halves are not
mistaken for one.
"""
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Employee, app, db                         # noqa: E402

KEY = 'margin_viewers_ff'

#: Nilesh, TGR, Bhavin, Sanjna — resolved against the employee master
#: on 2 November, and re-checked by --check every run, because a code
#: that is not an employee grants the right to nobody and hides the
#: numbers from everybody.
WANTED = [
    ('DIR12010', 'Nilesh Kumar Sinha'),
    ('DIR42010', 'Tg Ramalingam'),
    ('EMP3892025', 'Bhavin Vinodhbhai Jiilka'),
    ('EMP372011', 'Sanjna Vardhan'),
]

ACTOR = 'PROCAM-DECISIONS-02NOV'


def _name(code):
    emp = Employee.query.filter_by(emp_code=code).first()
    if emp is None:
        return '(NOT AN EMPLOYEE ON FILE)'
    return (emp.name or '') + ('' if emp.is_active else ' — INACTIVE')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()
    if len([a for a in (args.check, args.apply, args.down) if a]) != 1:
        raise SystemExit('Choose one: --check, --apply or --down.')

    with app.app_context():
        from app.services import admin_settings, audit

        print(f'database: {db.engine.url}\n')
        was = list(admin_settings.get(KEY) or [])
        wanted = [c for c, _ in WANTED]

        print('  who sees FF buy rates and margin today')
        for code in was:
            print(f'    {code:<12} {_name(code)}')
        if not was:
            print('    (nobody saved — the code default applies)')
        print('\n  who the decision names')
        for code, who in WANTED:
            mark = ' ' if code in was else '+'
            print(f'  {mark} {code:<12} {who} — {_name(code)}')
        losing = [c for c in was if c not in wanted]
        for code in losing:
            print(f'  - {code:<12} {_name(code)} — loses the view')

        missing = [c for c, _ in WANTED
                   if Employee.query.filter_by(emp_code=c).first() is None]
        if missing:
            raise SystemExit(
                '\n  !! not employees on file: ' + ', '.join(missing) +
                '\n  Fix the codes before granting anything.')

        if sorted(was) == sorted(wanted):
            print('\n  already exactly these four — nothing to do')
            if args.apply:
                return

        if args.check:
            print('\nCHECK ONLY — nothing written.')
            print('\n  TMS side, not done by this script: '
                  'FF_JOB.VIEW_BUY_RATES and FF_JOB.VIEW_MARGIN are '
                  'granted by role to every admin in the TMS. That is '
                  'a wider group than these four.')
            return

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            from app.models.audit import AuditEvent
            ev = (AuditEvent.query
                  .filter(AuditEvent.action == 'setting.changed',
                          AuditEvent.entity_id == KEY,
                          AuditEvent.actor == ACTOR)
                  .order_by(AuditEvent.id.desc()).first())
            old = (getattr(ev, 'old_value', None) or {}).get('value')
            if old is None:
                raise SystemExit('No audit row from this script to '
                                 'restore from.')
            admin_settings.save(KEY, old, actor=ACTOR)
            print(f'\n  put back to {json.dumps(old)}')
            return

        audit.record('setting.changed', 'setting', KEY,
                     old={'value': was}, new={'value': wanted},
                     actor=ACTOR,
                     reason='20 Oct decision, confirmed 2 Nov: FF buy '
                            'rates and margin to Nilesh, TGR, Bhavin '
                            'and Sanjna only',
                     commit=False)
        admin_settings.save(KEY, wanted, actor=ACTOR)
        print(f'\n  saved: {", ".join(wanted)}')
        if losing:
            print('  removed: ' + ', '.join(losing))
        print('\n  Still to do in the TMS: restrict '
              'FF_JOB.VIEW_BUY_RATES and FF_JOB.VIEW_MARGIN to these '
              'four. The role table there grants both to every admin.')


if __name__ == '__main__':
    main()
