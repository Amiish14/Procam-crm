"""
Quote / RFQ escalation — schema and configuration.

Adds
    escalation_log      one row per (RFQ, escalation level, recipient)
                        once it has been delivered. The unique key on it
                        is what stops the fifteen-minute sweep telling
                        the same person the same thing ninety-six times
                        a day.

Seeds
    the Master Data list `escalation_rule`, one item per level, each
    carrying its hours in `meta`. Seeded so an administrator can see and
    change the timings the day this ships rather than discovering an
    empty screen; the service re-seeds anything missing anyway, so
    running this twice changes nothing.

Nothing is backfilled. An escalation that was never sent cannot be
recorded as sent, and pretending otherwise would silence the first real
one. The sweep's backlog window (the `max_backlog` item, 30 days by
default) is what stops the first run shouting about historical RFQs.

Usage
    python scripts/2026_10_10_escalation.py --check
    python scripts/2026_10_10_escalation.py
    python scripts/2026_10_10_escalation.py --down --yes
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from sqlalchemy import inspect                            # noqa: E402

from app import app, db                                   # noqa: E402
from app.models.escalation import EscalationLog           # noqa: E402
from app.services import escalation                       # noqa: E402

TABLE = 'escalation_log'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        from app import Employee
        if not Employee.query.count():
            raise SystemExit('Refusing to run: not the production database.')
        exists = inspect(db.engine).has_table(TABLE)

        if args.down:
            if args.check or not exists:
                print(f'== DRY-RUN == WOULD drop '
                      f'{TABLE if exists else "nothing"}')
                print('  This loses the record of what was already sent, so '
                      '  the next sweep would send every live escalation '
                      '  again. The Master Data timings are left in place.')
                return
            if not args.yes:
                raise SystemExit('  Refusing without --yes.')
            EscalationLog.__table__.drop(bind=db.engine)
            print(f'  - {TABLE}')
            return

        if args.check:
            print('== DRY-RUN — nothing written ==')
            print(f'  WOULD create: '
                  f'{TABLE if not exists else "(already there)"}')
            from app.models.master_data import MasterItem
            have = {i.code for i in
                    MasterItem.query.filter_by(
                        list_key=escalation.LIST_KEY).all()}
            want = set(escalation.STAGE_KEYS) | {escalation.MAX_BACKLOG_KEY}
            print(f'  WOULD seed Master Data "{escalation.LIST_KEY}": '
                  f'{sorted(want - have) or "(already there)"}')
            print('  Nothing backfilled.')
            return

        if not exists:
            EscalationLog.__table__.create(bind=db.engine)
            print(f'  + {TABLE}')
        else:
            print(f'  = {TABLE} already there')
        seeded = escalation.ensure_rules(actor='migration')
        print(f'  + Master Data "{escalation.LIST_KEY}": {seeded} row(s)')
    print('  done.')


if __name__ == '__main__':
    main()
