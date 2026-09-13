"""
Daily Data Quality snapshot — the numbers the dashboard sparklines draw.

Measures every Data Quality check across the whole company and writes one
row per check for the day into data_quality_snapshots. Counts only: no
customer data is stored.

    python scripts/data_quality_snapshot.py
    python scripts/data_quality_snapshot.py --date 2026-09-30

Idempotent: a second run on the same day replaces that day's counts, so a
retried timer cannot draw a spike. Run daily by the
procam-crm-dq-snapshot timer (docs/operations/deploy/).

Exit codes: 0 all checks measured; 1 written, but at least one check
failed and was recorded as a gap; 2 nothing written (the table does not
exist yet, or a bad --date).
"""
import argparse
import os
import sys
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import app, db                                        # noqa: E402
from app.data_quality import service as dq                     # noqa: E402
from app.models.data_quality import DataQualitySnapshot        # noqa: E402


def _table_exists():
    from sqlalchemy import inspect
    return inspect(db.engine).has_table(DataQualitySnapshot.__tablename__)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[1])
    ap.add_argument('--date', help='snapshot date, YYYY-MM-DD (default today)')
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args(argv)
    try:
        day = date.fromisoformat(args.date) if args.date else date.today()
    except ValueError:
        print(f'not a date: {args.date!r} (expected YYYY-MM-DD)',
              file=sys.stderr)
        return 2

    with app.app_context():
        # The schema is not this script's to change: the table arrives
        # with its migration, and until then the job says so and stops.
        if not _table_exists():
            print('data_quality_snapshots does not exist yet — apply the '
                  'migration first. Nothing was written.', file=sys.stderr)
            return 2
        rows = dq.snapshot_all(today=day)

    failed = [r['key'] for r in rows if r['count'] is None]
    if not args.quiet:
        width = max(len(r['key']) for r in rows)
        for r in rows:
            shown = '—' if r['count'] is None else f'{r["count"]:,}'
            print(f'  {r["key"]:<{width}}  {shown:>8}')
    print(f'Data Quality snapshot for {day}: {len(rows)} checks written'
          + (f', {len(failed)} failed ({", ".join(failed)})' if failed
             else '') + '.')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
