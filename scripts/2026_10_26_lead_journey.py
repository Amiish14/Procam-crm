"""
CRM-LEAD-JOURNEY-01 (R4) — the funnel's columns and the targets.

    .venv/bin/python scripts/2026_10_26_lead_journey.py --check
    .venv/bin/python scripts/2026_10_26_lead_journey.py
    .venv/bin/python scripts/2026_10_26_lead_journey.py --backfill --apply
    .venv/bin/python scripts/2026_10_26_lead_journey.py --down --yes

Additive. The backfill sets `lead_channel` on past leads **only
where one of the four automatic rules fits** (§6.1) — an auction,
a director's forward, a repeat customer, or mail from outside.
Leads that fit none are left blank: a channel guessed for them
would feed a conversion rate that reads as measurement.
"""
import argparse
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect, text                      # noqa: E402

from app import Lead, app, db                             # noqa: E402
from app.models.rfq_target import RfqTarget as _T         # noqa: E402

COLUMNS = (
    ('leads', 'lead_channel', 'VARCHAR(40)'),
    ('leads', 'lead_channel_auto', 'BOOLEAN'),
    ('leads', 'expected_rfq_timing', 'VARCHAR(32)'),
    ('leads', 'capability_fit', 'VARCHAR(32)'),
    ('leads', 'rfq_expected_date', 'DATE'),
)
TABLE = 'rfq_targets'


def _cols(table):
    try:
        return {c['name'] for c in inspect(db.engine).get_columns(table)}
    except Exception:                                     # noqa: BLE001
        return set()


def backfill(*, apply=False, limit=0):
    from app.services import lead_journey as lj

    rows = (Lead.query
            .filter(Lead.lead_channel.is_(None),
                    Lead.is_archived.isnot(True))
            .order_by(Lead.id.desc()))
    if limit:
        rows = rows.limit(limit)
    found, blank = Counter(), 0
    for lead in rows.all():
        channel, _why = lj.detect_channel(lead)
        if not channel:
            blank += 1
            continue
        found[channel] += 1
        if apply:
            lj.set_channel(lead, actor='CRM-LEAD-JOURNEY-01')
    if apply:
        db.session.commit()
    return found, blank


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--backfill', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        absent = [(t, c) for t, c, _d in COLUMNS if c not in _cols(t)]
        try:
            here = TABLE in set(inspect(db.engine).get_table_names())
        except Exception:                                 # noqa: BLE001
            here = False
        print(f'  columns: {len(COLUMNS) - len(absent)} present, '
              f'{len(absent)} absent')
        print(f'  {TABLE}: {"present" if here else "absent"}')

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            if here:
                db.session.execute(text(f'DROP TABLE {TABLE}'))
                db.session.commit()
                print(f'  dropped {TABLE}')
            for tbl, col, _d in reversed(COLUMNS):
                if col not in _cols(tbl):
                    continue
                try:
                    db.session.execute(text(
                        f'ALTER TABLE {tbl} DROP COLUMN {col}'))
                    db.session.commit()
                    print(f'  dropped {tbl}.{col}')
                except Exception as exc:                  # noqa: BLE001
                    db.session.rollback()
                    print(f'  !! {tbl}.{col}: {str(exc)[:110]}')
            return

        if args.backfill:
            if absent:
                raise SystemExit('  add the columns first.')
            found, blank = backfill(apply=args.apply,
                                    limit=args.limit)
            print(f'\n  MODE: {"APPLY" if args.apply else "DRY RUN"}')
            for channel, n in found.most_common():
                print(f'    {channel:<34}{n:>6}')
            print(f'    {"(left blank — no rule fits)":<34}{blank:>6}')
            if not args.apply:
                print('\nDRY RUN — nothing written.')
            else:
                print(f'\n  {sum(found.values())} lead(s) given a '
                      f'channel. The rest stay blank rather than '
                      f'carrying a guess into the conversion maths.')
            return

        if args.check:
            print('\nCHECK ONLY — nothing written.')
            return

        for tbl, col, ddl in COLUMNS:
            if col in _cols(tbl):
                continue
            db.session.execute(text(
                f'ALTER TABLE {tbl} ADD COLUMN {col} {ddl}'))
            db.session.commit()
            print(f'  added {tbl}.{col}')
        if not here:
            _T.__table__.create(db.engine)
            print(f'  created {TABLE}')
        print('\n  Done. Run --backfill to see what the automatic '
              'rules would set.')


if __name__ == '__main__':
    main()
