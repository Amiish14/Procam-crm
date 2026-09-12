"""
Put back leads that were deleted, using a database backup.

About 72 leads were hard-deleted from production between 10 and 12
September.  Most were junk from the mailbox — "Unknown", "Global",
"Facebookmail" — but a handful were real, assigned, in-progress records,
including one at Quoted stage.  There was no audit row and no usable log,
so nobody can say whether they went deliberately.

This reads a backup, shows exactly what it would restore, and puts back
only the ids you name.  It never restores everything on its own: the
junk deletions were sensible housekeeping and should stay deleted.

Usage
    # what is missing, compared against a backup
    python scripts/2026_09_23_restore_leads_from_backup.py \\
        --backup procam_crm.db.bak-2026-09-10-0301 --list

    # only the ones worth having back
    python scripts/2026_09_23_restore_leads_from_backup.py \\
        --backup procam_crm.db.bak-2026-09-10-0301 \\
        --ids 3044,9539,10723,11110,11545 --check

    # then for real
    python scripts/2026_09_23_restore_leads_from_backup.py \\
        --backup procam_crm.db.bak-2026-09-10-0301 \\
        --ids 3044,9539,10723,11110,11545

Back up the live database first — this writes to it:
    cp procam_crm.db procam_crm.db.bak-$(date +%F-%H%M)

A restored lead keeps its original id where that id is still free. If
something else has since taken it — SQLite reuses the ids of deleted
rows — the lead comes back under a new id and the script says so.
"""
import argparse
import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, '.env'))
except ImportError:
    pass


def _live_path():
    url = os.environ.get('DATABASE_URL') or (
        'sqlite:///' + os.path.join(_ROOT, 'procam_crm.db'))
    if not url.startswith('sqlite:'):
        raise SystemExit('This tool only handles SQLite databases.')
    return '/' + url.split('sqlite:///')[-1].lstrip('/')


def _columns(conn, table):
    return [r[1] for r in conn.execute(f'PRAGMA table_info({table})')]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--backup', required=True, help='the backup to read')
    ap.add_argument('--list', action='store_true',
                    help='show every lead present in the backup and missing '
                         'from the live database')
    ap.add_argument('--ids', help='comma-separated lead ids to restore')
    ap.add_argument('--check', action='store_true', help='dry-run')
    args = ap.parse_args()

    backup = args.backup if os.path.isabs(args.backup) \
        else os.path.join(_ROOT, args.backup)
    if not os.path.isfile(backup):
        raise SystemExit(f'No such backup: {backup}')
    live = _live_path()
    print(f'backup: {backup}')
    print(f'live:   {live}')

    src = sqlite3.connect(f'file:{backup}?mode=ro', uri=True)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(live)
    dst.row_factory = sqlite3.Row

    if not dst.execute('SELECT COUNT(*) FROM employees').fetchone()[0]:
        raise SystemExit('Refusing to run: the live database has no '
                         'employees, so it is not production.')

    live_ids = {r[0] for r in dst.execute('SELECT id FROM leads')}
    missing = [r for r in src.execute('SELECT * FROM leads ORDER BY id')
               if r['id'] not in live_ids]

    if args.list or not args.ids:
        print(f'\n  {len(missing)} lead(s) in the backup are not in the '
              f'live database.\n')
        print(f'  {"id":<8}{"company":<32}{"stage":<18}{"owner":<12}created')
        for r in missing:
            owner = r['assigned_to'] or '-'
            worked = bool(r['assigned_to']) or (
                r['stage'] not in ('New Opportunity', 'New', None))
            mark = ' *' if worked else '  '
            print(f'{mark}{r["id"]:<8}{(r["company"] or "")[:30]:<32}'
                  f'{(r["stage"] or "")[:16]:<18}{owner[:10]:<12}'
                  f'{str(r["created_at"] or "")[:10]}')
        print('\n  * = had an owner or had moved past New — most likely to '
              'be a real loss\n    rather than mailbox junk.')
        if not args.ids:
            print('\n  Nothing restored. Re-run with --ids to choose.')
            return

    wanted = [int(x) for x in args.ids.split(',') if x.strip().isdigit()]
    by_id = {r['id']: r for r in missing}
    unknown = [i for i in wanted if i not in by_id]
    if unknown:
        print(f'\n  !! not in the backup, or already live: {unknown}')

    cols_live = set(_columns(dst, 'leads'))
    todo = [by_id[i] for i in wanted if i in by_id]
    print(f'\n  {"would restore" if args.check else "restoring"} '
          f'{len(todo)} lead(s):')

    restored = 0
    for row in todo:
        # Only columns that still exist — the schema has moved on since
        # some of these backups were taken.
        data = {k: row[k] for k in row.keys() if k in cols_live}
        note = ''
        if row['id'] in live_ids:
            data.pop('id', None)
            note = ' (id taken — restored under a new one)'
        print(f'    #{row["id"]:<7} {(row["company"] or "")[:30]:<32}'
              f'{(row["stage"] or "")[:16]}{note}')
        if args.check:
            continue
        placeholders = ', '.join('?' for _ in data)
        dst.execute(
            f'INSERT INTO leads ({", ".join(data)}) VALUES ({placeholders})',
            list(data.values()))
        restored += 1

    if args.check:
        print('\n== DRY-RUN — nothing written ==')
    else:
        dst.commit()
        print(f'\n  restored {restored} lead(s).')
        print('  Their activities, notes and attachments are NOT restored — '
              'those were\n  deleted separately and are in the backup if you '
              'need them too.')
    src.close()
    dst.close()


if __name__ == '__main__':
    main()
