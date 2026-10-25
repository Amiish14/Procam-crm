"""
CRM-QUOTE-REV-02 (R3) — revision roots.

    .venv/bin/python scripts/2026_10_26_quote_rev.py --check
    .venv/bin/python scripts/2026_10_26_quote_rev.py
    .venv/bin/python scripts/2026_10_26_quote_rev.py --backfill --apply
    .venv/bin/python scripts/2026_10_26_quote_rev.py --down --yes

Adds `quotes.root_quote_id`, `quotes.rev_no` and
`quotes.revision_reason`, then walks `parent_quote_id` to fill
the first two on any chain that already exists.

Production holds 0 quotes, so the backfill will report 0. It is
written anyway: the first real revision needs a root, and a
backfill nobody has run is one nobody can trust later.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect, text                      # noqa: E402

from app import app, db                                   # noqa: E402

COLUMNS = (('root_quote_id', 'INTEGER'),
           ('rev_no', 'INTEGER'),
           ('revision_reason', 'VARCHAR(120)'))
TABLE = 'quotes'
ACTION = 'quote.root_backfilled'


def _cols():
    try:
        return {c['name'] for c in inspect(db.engine).get_columns(TABLE)}
    except Exception:                                     # noqa: BLE001
        return set()


def plan():
    """[(quote, root_id, rev_no)] for every chain on file."""
    from app.models.quote import Quote
    from app.services import quote_rev

    out = []
    for quote in Quote.query.order_by(Quote.id).all():
        root = quote_rev.root_of(quote)
        root_id = root.id if root is not None else quote.id
        depth, node, seen = 0, quote, {quote.id}
        while node.parent_quote_id and node.parent_quote_id not in seen:
            seen.add(node.parent_quote_id)
            node = db.session.get(Quote, node.parent_quote_id)
            if node is None:
                break
            depth += 1
        if quote.root_quote_id == root_id and quote.rev_no == depth:
            continue
        out.append((quote, root_id, depth))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--backfill', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        have = _cols()
        absent = [c for c, _d in COLUMNS if c not in have]
        print(f'  columns: {len(COLUMNS) - len(absent)} present, '
              f'{len(absent)} absent')

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            for col, _d in reversed(COLUMNS):
                if col not in _cols():
                    continue
                try:
                    db.session.execute(text(
                        f'ALTER TABLE {TABLE} DROP COLUMN {col}'))
                    db.session.commit()
                    print(f'  dropped {col}')
                except Exception as exc:                  # noqa: BLE001
                    db.session.rollback()
                    print(f'  !! {col}: {str(exc)[:110]}')
            return

        if args.backfill or args.check:
            if absent:
                print('  (columns missing — add them first)')
            else:
                rows = plan()
                total = db.session.execute(text(
                    f'SELECT COUNT(*) FROM {TABLE}')).scalar()
                print(f'  quotes: {total}')
                print(f'  to set a root on: {len(rows)}')
                for quote, root_id, rev in rows[:12]:
                    print(f'    {quote.quote_number} -> root '
                          f'{root_id}, Rev-{rev:02d}')
                if args.backfill and args.apply:
                    from app.services import audit
                    for quote, root_id, rev in rows:
                        quote.root_quote_id = root_id
                        quote.rev_no = rev
                        audit.record(ACTION, 'quote', quote.id,
                                     new={'root_quote_id': root_id,
                                          'rev_no': rev},
                                     actor='CRM-QUOTE-REV-02',
                                     reason='revision chain walked '
                                            'from parent_quote_id')
                    db.session.commit()
                    print(f'\n  {len(rows)} quote(s) given a root.')
                    return
        if args.check or (args.backfill and not args.apply):
            print('\nCHECK ONLY — nothing written.')
            return

        for col, ddl in COLUMNS:
            if col in _cols():
                continue
            db.session.execute(text(
                f'ALTER TABLE {TABLE} ADD COLUMN {col} {ddl}'))
            db.session.commit()
            print(f'  added {TABLE}.{col}')
        print('\n  Done. Run --backfill --apply to walk existing '
              'chains.')


if __name__ == '__main__':
    main()
