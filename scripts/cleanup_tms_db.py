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

#: Fallback only. The authoritative list comes from
#: `scripts/crm_table_names.py`, which asks SQLAlchemy — this one was
#: typed out by hand and was missing four tables (`competitors`,
#: `project_accounts`, `project_contacts`,
#: `opportunity_source_links`), which is what a hand-kept list of
#: sixty names does. Pass --crm-tables and this is not used.
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



def _dependents(engine, present):
    """Who points at the CRM tables that are still here.

    The question that matters is not "is there a dependency" — four
    drops already said there is — but "is the dependent a TMS table".
    If every referring table is one of the CRM's own, this is an
    ordering problem and dropping children before parents solves it.
    If any TMS table points at a CRM table, that is a different
    problem and nothing should be dropped until somebody understands
    how it got there.
    """
    from sqlalchemy import text

    # Only the tables that are actually candidates for dropping. The
    # shared four are never dropped, so what points at them is not a
    # question this has to answer — and asking it anyway produced a
    # STOP over twenty-two TMS tables referencing the TMS's own
    # `projects`, which is simply how the TMS is built.
    crm_left = sorted(t for t in CRM_TABLES
                      if t in present and t not in NEVER_DROP)
    if not crm_left:
        print('no droppable CRM tables left — nothing to untangle.')
        return 0
    shared_present = sorted(t for t in NEVER_DROP if t in present)
    if shared_present:
        print(f'not examined (never dropped): '
              f'{", ".join(shared_present)}\n')

    sql = text("""
        SELECT tc.table_name      AS dependent_table,
               kcu.column_name    AS dependent_column,
               ccu.table_name     AS referenced_table,
               tc.constraint_name
          FROM information_schema.table_constraints tc
          JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name
           AND tc.table_schema = kcu.table_schema
          JOIN information_schema.constraint_column_usage ccu
            ON ccu.constraint_name = tc.constraint_name
           AND ccu.table_schema = tc.table_schema
         WHERE tc.constraint_type = 'FOREIGN KEY'
           AND tc.table_schema = 'public'
           AND ccu.table_name = ANY(:names)
         ORDER BY ccu.table_name, tc.table_name
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {'names': crm_left}).fetchall()

    print(f'FOREIGN KEYS POINTING AT THE {len(crm_left)} DROPPABLE CRM '
          f'TABLE(S)\n')
    foreign = []
    for dependent, column, referenced, constraint in rows:
        whose = 'CRM' if dependent in CRM_TABLES else 'NOT A CRM TABLE'
        if dependent not in CRM_TABLES:
            foreign.append((dependent, referenced, constraint))
        print(f'  {dependent}.{column} → {referenced}   [{whose}]')
    if not rows:
        print('  none — the drops failed for some other dependency '
              '(a view, perhaps). Investigate before forcing anything.')

    print('\nVERDICT')
    if foreign:
        print('  STOP. These dependents are not CRM tables:')
        for dependent, referenced, constraint in foreign:
            print(f'    {dependent} → {referenced} ({constraint})')
        print('  Something in the TMS references a CRM table. Do not '
              'drop anything until that is understood.')
        return 1
    print('  Every dependent is another CRM table, so this is only an '
          'ordering problem.')
    print('  Re-run with --apply --yes: the script now drops children '
          'before parents and will clear them.')
    return 0


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
    ap.add_argument('--crm-tables',
                    help='file of CRM table names, one per line, from '
                         'scripts/crm_table_names.py. Strongly '
                         'preferred over the built-in list.')
    ap.add_argument('--show-dependents', action='store_true',
                    help='name every foreign key pointing at the CRM '
                         'tables still present, and say whose table it '
                         'is on')
    args = ap.parse_args()

    url = _database_url(args.env_file)
    if not url:
        print('no DATABASE_URL in that file.')
        return 2
    print(f'database: {_safe(url)}')

    global CRM_TABLES
    if args.crm_tables:
        try:
            with open(args.crm_tables, encoding='utf-8') as fh:
                CRM_TABLES = tuple(sorted(
                    line.strip() for line in fh if line.strip()))
            print(f'CRM tables: {len(CRM_TABLES)} name(s) from '
                  f'{args.crm_tables}')
        except OSError as exc:
            print(f'could not read {args.crm_tables}: {exc}')
            return 2
    else:
        print(f'CRM tables: the built-in list of {len(CRM_TABLES)} — '
              f'incomplete. Use --crm-tables.')
    print()

    from sqlalchemy import create_engine, inspect, text

    engine = create_engine(url)
    insp = inspect(engine)
    present = set(insp.get_table_names())

    if args.show_dependents:
        return _dependents(engine, present)

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
    # Children before parents. The first run refused companies,
    # contacts, leads and opportunities because they reference one
    # another; dropping in this order clears them without CASCADE.
    order = {name: i for i, name in enumerate(
        ('opportunities', 'leads', 'contacts', 'companies'))}
    drop.sort(key=lambda pair: order.get(pair[0], -1))

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
