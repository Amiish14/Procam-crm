"""
Remove the CRM tables that were created in the TMS database by mistake.

On 2026-10-06 a CRM migration ran in a shell that still held the TMS's
DATABASE_URL, so `create_all` built the CRM's schema inside the TMS's
PostgreSQL. This takes those tables out again.

Three refusals, because this is a DROP against a production database
belonging to another application:

  * **only names the CRM owns.** The allow-list is explicit. A table
    not on it is never dropped, whatever it looks like.
  * **never the four shared names.** `notifications`, `projects`,
    `task_definitions` and `task_instances` are defined by both
    applications and in this database they hold the TMS's data —
    319, 572, 25 and 2,887 rows of it. They are excluded by name and
    by a row check.
  * **never a table with unexpected rows.** Everything the CRM
    created here is empty apart from the bootstrap employee and two
    audit rows. Anything else holding data is somebody's, and this
    stops rather than guessing whose.

    /var/www/procam-lr/venv/bin/python scripts/cleanup_tms_db.py --check
    /var/www/procam-lr/venv/bin/python scripts/cleanup_tms_db.py --apply --yes

Standalone: imports no CRM code, so it runs under the TMS's own
interpreter, which has the PostgreSQL driver.
"""
import argparse
import re
import sys

#: Defined by both applications. In this database they are the TMS's.
#: Never dropped, under any flag.
NEVER_DROP = ('notifications', 'projects', 'task_definitions',
              'task_instances')

#: The CRM tables found in the TMS database on 2026-10-06. An explicit
#: list rather than a pattern: a pattern that matched one TMS table
#: would be unrecoverable.
CRM_TABLES = (
    'account_activities', 'account_assignments',
    'account_relationship_tags', 'account_stage_history', 'audit_events',
    'client_restriction_attachments', 'client_restriction_events',
    'client_restrictions', 'companies', 'contacts',
    'email_classifications', 'email_events', 'employees',
    'import_batches', 'lead_activities', 'lead_assignment_history',
    'lead_attachments', 'lead_emails', 'lead_notes',
    'lead_stage_history', 'leads', 'opportunities', 'outreach_drafts',
    'overseas_agents', 'vendor_domains',
)

#: Rows these are expected to hold, from the CRM's own bootstrap. More
#: than this and the script stops.
EXPECTED_ROWS = {'employees': 1, 'audit_events': 2}


def _database_url(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if line.startswith('DATABASE_URL=') and '=' in line:
                    value = line.partition('=')[2].strip()
                    if len(value) >= 2 and value[0] == value[-1] in ('"', "'"):
                        value = value[1:-1]
                    return value
    except OSError as exc:
        print(f'could not read {path}: {exc}')
    return None


def _safe(url):
    return re.sub(r'://([^:/@]+):[^@]*@', r'://\1:***@', url or '')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--env-file', default='/var/www/procam-lr/.env')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--yes', action='store_true')
    # Accepted and the default anyway. The docstring offers it and the
    # other migration scripts in this repository all take it, so a
    # parser that rejects it is a script that argues with its own
    # instructions at the moment somebody is being careful.
    ap.add_argument('--check', action='store_true',
                    help='list what would be dropped and stop (default)')
    args = ap.parse_args()

    url = _database_url(args.env_file)
    if not url:
        print('no DATABASE_URL in that file.')
        return 2
    print(f'database: {_safe(url)}\n')

    from sqlalchemy import create_engine, inspect, text

    engine = create_engine(url)
    insp = inspect(engine)
    present = set(insp.get_table_names())

    drop, keep, refuse = [], [], []
    for name in CRM_TABLES:
        if name in NEVER_DROP:
            refuse.append((name, 'shared with the TMS'))
            continue
        if name not in present:
            continue
        with engine.connect() as conn:
            rows = conn.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar()
        allowed = EXPECTED_ROWS.get(name, 0)
        if rows > allowed:
            refuse.append((name, f'{rows} rows, expected at most {allowed}'))
        else:
            drop.append((name, rows))

    for name in NEVER_DROP:
        if name in present:
            with engine.connect() as conn:
                rows = conn.execute(
                    text(f'SELECT COUNT(*) FROM "{name}"')).scalar()
            keep.append((name, rows))

    print('WOULD DROP' if not args.apply else 'DROPPING')
    for name, rows in drop:
        print(f'  {name:<34} {rows:>6} row(s)')
    print(f'  {len(drop)} table(s)\n')

    print('KEEPING — the TMS owns these')
    for name, rows in keep:
        print(f'  {name:<34} {rows:>6} row(s)')

    if refuse:
        print('\nREFUSING')
        for name, why in refuse:
            print(f'  {name:<34} {why}')
        print('  Not dropped. Work out whose these are first.')

    if not args.apply:
        print('\n== CHECK — nothing dropped ==')
        return 0
    if not args.yes:
        raise SystemExit('Refusing to drop without --yes.')

    # RESTRICT, not CASCADE. CASCADE would also remove anything
    # depending on these tables — a foreign key from a TMS table, a
    # view — quietly and without naming it. Nothing here should have
    # a dependent, so a drop that fails for that reason is telling us
    # something we need to hear rather than something to force past.
    done, blocked = 0, []
    for name, _rows in drop:
        try:
            with engine.begin() as conn:
                conn.execute(text(f'DROP TABLE "{name}" RESTRICT'))
            done += 1
            print(f'  - {name}')
        except Exception as exc:                              # noqa: BLE001
            blocked.append((name, str(exc).split('\n')[0][:160]))
            print(f'  ! {name}: kept — something depends on it')

    print(f'\n{done} table(s) dropped. The TMS\'s own tables are '
          f'untouched.')
    if blocked:
        print('\nNOT DROPPED — each of these has a dependent object:')
        for name, why in blocked:
            print(f'  {name}\n    {why}')
        print('  Find out what depends on them before forcing anything. '
              'Do not re-run this with CASCADE.')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
