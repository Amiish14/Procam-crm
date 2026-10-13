"""
Email administration: vertical heads, recipient policy, switches.

Three tables and two columns.

    vertical_head        which vertical belongs to whom
    vertical_alias       the spellings leads actually carry, mapped
                         onto the few verticals the business manages
    notification_policy  the To and CC roles per event

    email_outbox.policy_version   which policy chose these addresses
    email_outbox.smtp_code        what the mail server actually said

Nothing is guessed. The seeds come from what is already in the
database:

    a `vertical_head` row per distinct value in `employees.vertical`,
    with the head taken from `employees.is_vertical_head` — the flag
    the CRM reads today — so the people being copied this morning are
    the people being copied tomorrow;

    `vertical_alias` rows only where the CRM already holds the mapping
    (`app/copilot/vocabulary.CRM_SERVICE`, itself derived from the
    Master Data service list). Every other unmapped spelling is
    *printed*, not invented: which vertical "Heavy Cargo" belongs to
    is a business decision and this script is not entitled to make it.

    no `notification_policy` rows at all. An absent row means the
    built-in matrix is in force, which is what is in force today, so
    seeding rows would be seeding a change nobody asked for. The
    screen writes a row the first time somebody edits an event.

    `always_copy` seeded with DIR12010, as the brief asks, and the
    standing copy switched on.

    FEATURE_DAILY_BRIEF switched on in the database, as the brief
    asks. Every other switch is left alone so the environment keeps
    deciding it.

    .venv/bin/python scripts/2026_10_15_email_admin.py --check
    .venv/bin/python scripts/2026_10_15_email_admin.py
    .venv/bin/python scripts/2026_10_15_email_admin.py --down --yes
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect, text                      # noqa: E402

from app import Employee, app, db                         # noqa: E402
from app.models.email_admin import (FALLBACK_VERTICAL,    # noqa: E402
                                    NotificationPolicy, VerticalAlias,
                                    VerticalHead)

TABLES = (('vertical_head', VerticalHead),
          ('vertical_alias', VerticalAlias),
          ('notification_policy', NotificationPolicy))

COLUMNS = (('email_outbox', 'policy_version', 'INTEGER'),
           ('email_outbox', 'smtp_code', 'INTEGER'))

#: Seeded on the standing copy list, because the brief names them.
ALWAYS_COPY = ['DIR12010']


def _managed_verticals():
    """The verticals the business manages, from the employee master."""
    rows = db.session.execute(text(
        'SELECT DISTINCT vertical FROM employees '
        "WHERE vertical IS NOT NULL AND TRIM(vertical) <> ''")).fetchall()
    out = sorted({(r[0] or '').strip() for r in rows if (r[0] or '').strip()})
    if FALLBACK_VERTICAL not in out:
        # The fallback has to exist as a row, or an unmapped vertical
        # resolves to a vertical that is not in the master and reaches
        # nobody — which is the bug this release is fixing.
        out.append(FALLBACK_VERTICAL)
    return out


def _heads_today():
    """{vertical: emp_code} from the flag the CRM reads today."""
    out = {}
    for emp in (Employee.query
                .filter(Employee.is_vertical_head.is_(True))
                .filter(Employee.is_active.isnot(False))
                .order_by(Employee.id).all()):
        vertical = (emp.vertical or '').strip()
        if vertical and vertical not in out and emp.emp_code:
            out[vertical] = emp.emp_code
    return out


def _lead_verticals():
    """{value: count} for the verticals leads actually carry."""
    rows = db.session.execute(text(
        'SELECT procam_vertical, COUNT(*) FROM leads '
        "WHERE procam_vertical IS NOT NULL AND TRIM(procam_vertical) <> '' "
        'GROUP BY procam_vertical ORDER BY COUNT(*) DESC')).fetchall()
    return {(r[0] or '').strip(): int(r[1] or 0) for r in rows
            if (r[0] or '').strip()}


def _known_aliases(managed):
    """Aliases the CRM already holds a mapping for. {alias: vertical}."""
    try:
        from app.copilot.vocabulary import CRM_SERVICE
    except Exception:
        return {}
    out = {}
    for spoken, service in (CRM_SERVICE or {}).items():
        if not service or service not in managed:
            continue
        if spoken.strip() and spoken.strip() != service:
            out[spoken.strip()] = service
    return out


def _has_column(table, column):
    try:
        cols = {c['name'] for c in inspect(db.engine).get_columns(table)}
    except Exception:
        return False
    return column in cols


def _add_columns(dry):
    for table, column, ddl in COLUMNS:
        if not inspect(db.engine).has_table(table):
            print(f'  . {table} does not exist — skipping {column}')
            continue
        if _has_column(table, column):
            print(f'  . {table}.{column} is already there')
            continue
        if dry:
            print(f'  WOULD add {table}.{column} {ddl}')
            continue
        db.session.execute(text(
            f'ALTER TABLE {table} ADD COLUMN {column} {ddl}'))
        db.session.commit()
        print(f'  + {table}.{column}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        if not Employee.query.count():
            raise SystemExit('Refusing to run: not the production database.')

        insp = inspect(db.engine)
        present = {name: insp.has_table(name) for name, _m in TABLES}

        # ── down ──────────────────────────────────────────────────────
        if args.down:
            if args.check:
                for name, _m in TABLES:
                    print(f'== DRY-RUN == WOULD drop '
                          f'{name if present[name] else "nothing (" + name + ")"}')
                print('  The two email_outbox columns are left in place. '
                      'They hold what the mail server said about mail that '
                      'has already been sent, and dropping them loses that '
                      'and nothing else is gained.')
                print('  Dropping vertical_head returns the CRM to reading '
                      'employees.is_vertical_head alone, which still holds '
                      'the same people — this migration keeps the two in '
                      'step.')
                return
            if not args.yes:
                raise SystemExit('  Refusing without --yes.')
            for name, model in TABLES:
                if present[name]:
                    model.__table__.drop(bind=db.engine)
                    print(f'  - {name}')
            print('  . email_outbox.policy_version and .smtp_code kept')
            return

        # ── up ────────────────────────────────────────────────────────
        managed = _managed_verticals()
        heads = _heads_today()
        aliases = _known_aliases(managed)
        lead_values = _lead_verticals()

        if args.check:
            print('== DRY-RUN — nothing written ==')
            for name, _m in TABLES:
                print(f'  {"(already there)" if present[name] else "WOULD create"}'
                      f'  {name}')
            _add_columns(dry=True)
            print(f'\n  WOULD seed {len(managed)} vertical_head row(s), '
                  f'{len(heads)} with a head already named:')
            for vertical in managed:
                who = heads.get(vertical)
                print(f'    {vertical:<24} {who or "— nobody named —"}')
            print(f'\n  WOULD seed {len(aliases)} alias row(s) the CRM '
                  f'already holds the mapping for:')
            for alias, vertical in sorted(aliases.items()):
                print(f'    {alias:<24} → {vertical}')
            unmapped = {v: n for v, n in lead_values.items()
                        if v not in managed and v not in aliases}
            if unmapped:
                print(f'\n  {len(unmapped)} vertical(s) on live leads map to '
                      f'nothing, and this script will NOT guess:')
                for value, count in sorted(unmapped.items(),
                                           key=lambda kv: -kv[1]):
                    print(f'    {value:<24} {count:>6} leads')
                print('    Map each one in Admin → Email → Vertical heads. '
                      'Until then they fall back to '
                      f'{FALLBACK_VERTICAL}, so a notification still '
                      'reaches somebody senior rather than nobody.')
            print(f'\n  WOULD seed always_copy = {ALWAYS_COPY} and turn '
                  f'FEATURE_DAILY_BRIEF on in the database.')
            print('  WOULD seed no notification_policy rows — an absent '
                  'row means the built-in matrix is in force, which is '
                  'what is in force today.')
            return

        for name, model in TABLES:
            if not present[name]:
                db.metadata.create_all(bind=db.engine,
                                       tables=[model.__table__])
                print(f'  + {name}')
            else:
                print(f'  . {name} is already there')
        _add_columns(dry=False)

        added = 0
        for vertical in managed:
            if VerticalHead.query.filter_by(vertical=vertical).first():
                continue
            db.session.add(VerticalHead(
                vertical=vertical,
                head_emp_code=heads.get(vertical),
                active=True,
                updated_by='migration',
                note=('head taken from employees.is_vertical_head'
                      if heads.get(vertical) else
                      'no head named yet — notifications reach the owner '
                      'and nobody above them')))
            added += 1
        db.session.commit()
        print(f'  + {added} vertical_head row(s)')

        added = 0
        for alias, vertical in sorted(aliases.items()):
            if VerticalAlias.query.filter_by(alias=alias).first():
                continue
            db.session.add(VerticalAlias(alias=alias, vertical=vertical,
                                         updated_by='migration'))
            added += 1
        db.session.commit()
        print(f'  + {added} vertical_alias row(s)')

        from app.models.integration import AppSetting
        from app.services import admin_settings, flags, vertical_heads

        # Written as a real row rather than left on the built-in
        # default, so the screen shows what is actually in force and so
        # an administrator who later clears the list is clearing
        # something. `get` falls back to the default when no row
        # exists, which would have made "already set" true on a
        # database where nothing was ever set.
        if AppSetting.query.filter_by(key='always_copy').first() is None:
            admin_settings.save('always_copy', ALWAYS_COPY, actor='migration')
            admin_settings.save('always_copy_enabled', True, actor='migration')
            print(f'  + always_copy = {ALWAYS_COPY}')
        else:
            print(f'  . always_copy already set to '
                  f'{admin_settings.get("always_copy")} — left alone')
        flags.set('FEATURE_DAILY_BRIEF', True, actor='migration')
        print('  + FEATURE_DAILY_BRIEF on (in the database)')

        set_now, cleared = vertical_heads.sync_employee_flags(actor='migration')
        print(f'  . employees.is_vertical_head: {len(set_now)} set, '
              f'{len(cleared)} cleared — the flag and the master now agree')

        unmapped = vertical_heads.unmapped_verticals()
        if unmapped:
            print(f'\n  {len(unmapped)} vertical(s) on live records map to '
                  f'nothing. Map them in Admin → Email → Vertical heads:')
            for value, count in unmapped:
                print(f'    {value:<24} {count:>6} records')
        print('\nDone. Restart the service: this release adds routes, '
              'templates and models, and a running worker has none of '
              'them.\n    sudo systemctl restart procam-crm'
              '\n\n  (The switches themselves are read from the database '
              'and need no restart after that — but the code that reads '
              'them does.)')


if __name__ == '__main__':
    main()
