"""
Find attachment files on disk that no row points at.

Two things leave them behind. The webhook path used to save files and
write no row at all, which is the defect this release fixes. And the
back-fill, before it learned to reuse an identical file, wrote a second
copy beside the first and pointed the row at the copy.

This only reports. Deleting a file the CRM cannot see is still deleting
a client's document, so it prints what it found and what it would
recover, and `--delete` has to be asked for on purpose.

    .venv/bin/python scripts/find_orphan_attachments.py
    .venv/bin/python scripts/find_orphan_attachments.py --delete --yes
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import LeadAttachment, app, db                   # noqa: E402


def _human(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:,.1f} {unit}'
        n /= 1024


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--delete', action='store_true')
    ap.add_argument('--yes', action='store_true')
    ap.add_argument('--show', type=int, default=20)
    args = ap.parse_args()

    with app.app_context():
        from email_ingest import attachments as att_mod
        root = att_mod.STORAGE_ROOT
        print(f'storage root: {root}')
        if not os.path.isdir(root):
            print('  the storage root does not exist — nothing to do.')
            return 0

        known = {r[0] for r in db.session.query(
            LeadAttachment.storage_path).all() if r[0]}
        # The originals are in an `original/` directory under each lead
        # and are tracked separately; they are not orphans.
        from app.models.mailops import LeadRawEmail
        known |= {r[0] for r in db.session.query(
            LeadRawEmail.storage_path).all() if r[0]}
        print(f'{len(known):,} file(s) are referenced by a row')

        orphans, total = [], 0
        for base, _dirs, files in os.walk(root):
            for name in files:
                path = os.path.join(base, name)
                if path in known:
                    continue
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                orphans.append((path, size))
                total += size

        print(f'{len(orphans):,} orphan file(s), {_human(total)}')
        for path, size in sorted(orphans, key=lambda p: -p[1])[:args.show]:
            print(f'  {_human(size):>10}  {path}')
        if len(orphans) > args.show:
            print(f'  … and {len(orphans) - args.show:,} more')

        if not args.delete:
            print('\n== REPORT ONLY — nothing deleted ==')
            print('Run the back-fill first: a file with no row today may '
                  'simply be one nothing has claimed yet, and the '
                  'back-fill is what claims it.')
            return 0
        if not args.yes:
            raise SystemExit('Refusing without --yes.')
        gone = 0
        for path, _size in orphans:
            try:
                os.remove(path)
                gone += 1
            except OSError as exc:
                print(f'  could not remove {path}: {exc}')
        print(f'\n{gone:,} file(s) removed, {_human(total)} recovered.')
        return 0


if __name__ == '__main__':
    sys.exit(main())
