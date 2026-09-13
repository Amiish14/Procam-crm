"""
Back up the live SQLite database safely, and prove the copy is good.

    .venv/bin/python scripts/backup_database.py
    .venv/bin/python scripts/backup_database.py --label pre-deploy
    .venv/bin/python scripts/backup_database.py --keep 30     # prune older

Why not `cp procam_crm.db backups/…`: a copy taken while gunicorn is
writing can capture half a transaction and be unreadable exactly when it
is needed. This uses SQLite's online backup API, which copies a
consistent snapshot while the app keeps running, then opens the copy and
runs an integrity check before declaring success.

The backup is written 0600 (it holds every customer record) into
backups/, which git ignores. Pruning only happens when --keep is given,
and only ever removes files this script created (procam_crm-*.db).

Exit status 0 only when the backup exists and passed its check.
"""
import argparse
import glob
import os
import sqlite3
import sys
import time
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, '.env'))
except ImportError:
    pass


def live_path():
    url = os.environ.get('DATABASE_URL') or (
        'sqlite:///' + os.path.join(_ROOT, 'procam_crm.db'))
    if not url.startswith('sqlite:///'):
        raise SystemExit('DATABASE_URL is not SQLite — use the database '
                         'server\'s own backup tooling')
    return url[len('sqlite:///'):]


def backup(src, folder, label=''):
    if not os.path.exists(src):
        raise SystemExit(f'database not found: {src}')
    os.makedirs(folder, mode=0o700, exist_ok=True)
    stamp = datetime.now().strftime('%Y-%m-%d-%H%M%S')
    name = f'procam_crm-{stamp}' + (f'-{label}' if label else '') + '.db'
    dest = os.path.join(folder, name)
    t0 = time.time()
    # create 0600 before SQLite writes a byte into it
    os.close(os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    source = sqlite3.connect(f'file:{src}?mode=ro', uri=True)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    check = sqlite3.connect(f'file:{dest}?mode=ro', uri=True)
    try:
        ok = check.execute('PRAGMA integrity_check').fetchone()[0]
        leads = check.execute('SELECT COUNT(*) FROM leads').fetchone()[0] \
            if check.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                             "AND name='leads'").fetchone() else None
    finally:
        check.close()
    return {'path': dest, 'ok': ok == 'ok', 'integrity': ok,
            'bytes': os.path.getsize(dest), 'leads': leads,
            'seconds': round(time.time() - t0, 2)}


def prune(folder, keep):
    files = sorted(glob.glob(os.path.join(folder, 'procam_crm-*.db')),
                   key=os.path.getmtime, reverse=True)
    gone = files[keep:]
    for f in gone:
        os.remove(f)
    return gone


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--label', default='',
                    help='appended to the file name, e.g. pre-deploy')
    ap.add_argument('--dir', default=os.path.join(_ROOT, 'backups'))
    ap.add_argument('--keep', type=int,
                    help='after a GOOD backup, delete all but the newest N '
                         'files this script created')
    args = ap.parse_args()
    label = ''.join(c for c in args.label if c.isalnum() or c in '-_')[:40]
    r = backup(live_path(), args.dir, label)
    print(f'  backup   {r["path"]}')
    print(f'  size     {r["bytes"] / 1_048_576:.1f} MB · leads {r["leads"]}'
          f' · {r["seconds"]} s')
    print(f'  check    {r["integrity"]}')
    if not r['ok']:
        print('  BACKUP FAILED ITS INTEGRITY CHECK — do not rely on it')
        return 1
    if args.keep:
        if args.keep < 3:
            raise SystemExit('--keep must be at least 3')
        for f in prune(args.dir, args.keep):
            print(f'  pruned   {os.path.basename(f)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
