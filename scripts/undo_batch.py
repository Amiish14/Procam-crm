"""
Put a bulk change back — the one command §8 asks for.

    .venv/bin/python scripts/undo_batch.py --list
    .venv/bin/python scripts/undo_batch.py --batch NAME --check
    .venv/bin/python scripts/undo_batch.py --batch NAME --apply

Every bulk change in CRM-COCKPIT-02 writes one row per field it
touched to `crm_bulk_change_log`, with the old value beside the new
one. This reads that back and writes the old values in, newest row
first so a field changed twice in one run ends where it started.

Two things it refuses to do, both on purpose:

  * it will not overwrite a later edit. If the field no longer holds
    the value the batch wrote, somebody has changed it since, and
    their decision stands — the row is reported and skipped;
  * it will not undo twice. Each restored row is stamped, so a second
    run of the same batch does nothing rather than undoing the undo.

`--check` is the default and prints exactly what would be written.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import app, db                                   # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--list', action='store_true',
                    help='the batches on file, newest first')
    ap.add_argument('--batch', default=None)
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--actor', default='CRM-COCKPIT-02-undo')
    args = ap.parse_args()

    import app.models.cockpit02                           # noqa: F401
    from app.services import bulk_log

    with app.app_context():
        print(f'database: {db.engine.url}')

        if args.list or not args.batch:
            rows = bulk_log.batches()
            if not rows:
                print('\n  no bulk changes on file.')
                return 0
            print(f'\n  {len(rows)} batch(es), newest first\n')
            print(f'  {"batch":<52} {"rows":>6} {"undone":>7}  last run')
            for name, total, undone, _first, last, note in rows:
                print(f'  {name:<52} {total:>6} {undone:>7}  '
                      f'{str(last)[:16]}')
                if note:
                    print(f'      {note[:88]}')
            if not args.batch:
                print('\n  --batch NAME --check to see what an undo '
                      'would write.')
            return 0

        rows = bulk_log.rows_of(args.batch, pending_only=True)
        done = len(bulk_log.rows_of(args.batch, pending_only=False)) \
            - len(rows)
        print(f'\n  batch            {args.batch}')
        print(f'  rows to put back  {len(rows)}')
        print(f'  already undone    {done}')
        if not rows:
            print('\n  Nothing to do.')
            return 0

        by_field = {}
        for row in rows:
            by_field[row.field] = by_field.get(row.field, 0) + 1
        print('\n  by field')
        for field, n in sorted(by_field.items(), key=lambda kv: -kv[1]):
            print(f'    {field:<28} {n}')

        print('\n  a sample of what would be written back:')
        for row in rows[:10]:
            was = '(empty)' if row.was_null else repr(row.old_value)
            print(f'    {row.entity_type}#{row.entity_id}.{row.field}: '
                  f'{row.new_value!r} -> {was}')

        if not args.apply:
            print('\nCHECK ONLY — nothing written. Re-run with --apply.')
            return 0

        restored, skipped, problems = bulk_log.undo(args.batch,
                                                    actor=args.actor)
        try:
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'\n  !! ROLLED BACK — nothing was written: '
                  f'{type(exc).__name__}: {str(exc)[:200]}')
            return 1
        print(f'\n  {restored} field(s) put back.')
        print(f'  {skipped} skipped.')
        for line in problems[:20]:
            print(f'      {line}')
        if len(problems) > 20:
            print(f'      ... and {len(problems) - 20} more')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
