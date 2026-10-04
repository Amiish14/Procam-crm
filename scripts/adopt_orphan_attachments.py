"""
Give the files already on disk the rows they never got.

The webhook path saved every attachment and wrote no `lead_attachments`
row, so on production 2,847 files — 905 MB of drawings, BOQs, packing
lists and gate passes — are sitting in each lead's own directory with
nothing able to serve them.

`backfill_rfq_capture.py` recovers them properly, by re-fetching from
the mailbox, which also gets the original `.eml`. But it can only do
that while Microsoft still has the message. For anything older the
file on disk is the only copy left, and it is already named, already
filtered, and already in a directory named after its lead. Adopting it
needs no network at all.

So: run the back-fill first, for everything it can reach, and run this
afterwards for the remainder.

What it will not do
    It never invents a lead. A directory whose lead has been deleted is
    reported and left alone — those files are what `--delete` on
    find_orphan_attachments.py is for, and that is a separate decision.
    It never adopts a file twice, and it skips a file that looks like a
    copy of one already recorded for that lead (same name and size),
    because two rows for one document means the drawer offers the same
    drawing twice.

    .venv/bin/python scripts/adopt_orphan_attachments.py
    .venv/bin/python scripts/adopt_orphan_attachments.py --apply --yes
    .venv/bin/python scripts/adopt_orphan_attachments.py --apply --yes \\
        --exclude-ext .gif,.png --min-bytes 20480
"""
import argparse
import mimetypes
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, LeadAttachment, app, db             # noqa: E402

#: Written by raw_mime.py and tracked by lead_raw_emails, not here.
ORIGINALS_DIR = 'original'


def _human(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:,.1f} {unit}'
        n /= 1024


def _scan(root, exclude_ext, min_bytes):
    """(adoptable, skipped) — walk the storage root once."""
    adoptable, skipped = [], Counter()

    known_paths = {r[0] for r in db.session.query(
        LeadAttachment.storage_path).all() if r[0]}
    # (lead_id, filename, size) of everything already recorded, so a
    # suffixed copy of a file that already has a row is not adopted as
    # a second document.
    known_files = {(r[0], os.path.basename(r[1] or ''), r[2])
                   for r in db.session.query(
                       LeadAttachment.lead_id, LeadAttachment.storage_path,
                       LeadAttachment.size_bytes).all()}
    live_leads = {r[0] for r in db.session.query(Lead.id).all()}

    for entry in sorted(os.listdir(root)):
        lead_dir = os.path.join(root, entry)
        if not os.path.isdir(lead_dir) or not entry.isdigit():
            continue
        lead_id = int(entry)
        for name in sorted(os.listdir(lead_dir)):
            path = os.path.join(lead_dir, name)
            if name == ORIGINALS_DIR or os.path.isdir(path):
                continue
            if path in known_paths:
                skipped['already recorded'] += 1
                continue
            if lead_id not in live_leads:
                skipped['the lead no longer exists'] += 1
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                skipped['unreadable'] += 1
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in exclude_ext:
                skipped[f'excluded extension ({ext})'] += 1
                continue
            if size < min_bytes:
                skipped['under the size floor'] += 1
                continue
            if (lead_id, name, size) in known_files:
                skipped['a copy of a file already recorded'] += 1
                continue
            # A suffixed copy — BOQ-1.pdf beside a recorded BOQ.pdf of
            # the same size — is the same document under another name.
            base, dot_ext = os.path.splitext(name)
            if '-' in base and base.rsplit('-', 1)[1].isdigit():
                original = base.rsplit('-', 1)[0] + dot_ext
                if (lead_id, original, size) in known_files:
                    skipped['a numbered copy of a recorded file'] += 1
                    continue
            adoptable.append((lead_id, path, name, size))
    return adoptable, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--yes', action='store_true')
    ap.add_argument('--exclude-ext', default='',
                    help='comma-separated, e.g. .gif,.png')
    ap.add_argument('--min-bytes', type=int, default=0)
    ap.add_argument('--show', type=int, default=15)
    args = ap.parse_args()

    exclude = {e.strip().lower() for e in args.exclude_ext.split(',')
               if e.strip()}

    with app.app_context():
        from email_ingest import attachments as att_mod
        root = att_mod.STORAGE_ROOT
        print(f'storage root: {root}')
        if not os.path.isdir(root):
            print('  nothing there.')
            return 0

        adoptable, skipped = _scan(root, exclude, args.min_bytes)
        total = sum(s for _l, _p, _n, s in adoptable)
        by_ext = Counter(os.path.splitext(n)[1].lower() or '(none)'
                         for _l, _p, n, _s in adoptable)
        leads = len({l for l, _p, _n, _s in adoptable})

        print(f'\n{len(adoptable):,} file(s) to adopt across {leads:,} '
              f'lead(s), {_human(total)}')
        for ext, count in by_ext.most_common(12):
            print(f'  {ext:>8}  {count:,}')
        if skipped:
            print('\nnot adopted:')
            for why, count in skipped.most_common():
                print(f'  {count:,}  {why}')

        if not args.apply:
            print('\nfirst few:')
            for lead_id, path, name, size in adoptable[:args.show]:
                print(f'  lead {lead_id:<6} {_human(size):>9}  {name}')
            print('\n== DRY-RUN — nothing written ==')
            print('Run backfill_rfq_capture.py first where you can: it '
                  'recovers the original email as well, and this cannot.')
            return 0

        if not args.yes:
            raise SystemExit('Refusing without --yes.')

        from app.services import audit
        added = 0
        for lead_id, path, name, size in adoptable:
            ctype = mimetypes.guess_type(name)[0] or 'application/octet-stream'
            db.session.add(LeadAttachment(
                lead_id=lead_id, filename=name, content_type=ctype,
                size_bytes=size, storage_path=path, source='email'))
            added += 1
            if added % 200 == 0:
                db.session.commit()
                print(f'  … {added:,}')
        audit.record('attachments.adopted', 'lead_attachments', None,
                     new={'count': added, 'bytes': total, 'leads': leads},
                     actor='system',
                     reason='files saved by the ingest that never got a row')
        db.session.commit()
        print(f'\n{added:,} file(s) adopted, {_human(total)} now reachable.')
        return 0


if __name__ == '__main__':
    sys.exit(main())
