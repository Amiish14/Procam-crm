"""
Escalation becomes three columns on the task, not a second row.

    .venv/bin/python scripts/2026_10_19_task_escalation.py --check
    .venv/bin/python scripts/2026_10_19_task_escalation.py
    .venv/bin/python scripts/2026_10_19_task_escalation.py --down --yes

    task_instances.escalated     INTEGER/BOOLEAN, default 0
    task_instances.escalated_to  VARCHAR(40)
    task_instances.escalated_at  TIMESTAMP

Additive only. The merge of the existing twins is a separate script,
`scripts/merge_escalated_tasks.py`, because adding a column and
rewriting 3,659 rows are different decisions and should be able to
fail separately.

The same autoheal caveat as every migration here: `init_db()` adds
missing columns on import, so this reports "already present" when run
from the project. It still matters — it adds them under the old code,
before the restart.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect, text                      # noqa: E402

from app import app, db                                   # noqa: E402

TABLE = 'task_instances'
COLUMNS = (('escalated', 'INTEGER DEFAULT 0', 'BOOLEAN DEFAULT FALSE'),
           ('escalated_to', 'VARCHAR(40)', 'VARCHAR(40)'),
           ('escalated_at', 'TIMESTAMP', 'TIMESTAMP'))


def _present():
    return {c['name'] for c in inspect(db.engine).get_columns(TABLE)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    with app.app_context():
        sqlite = db.engine.dialect.name == 'sqlite'
        have = _present()
        print(f'database: {db.engine.url}')
        for name, _s, _p in COLUMNS:
            print(f'  {TABLE}.{name}: '
                  + ('present' if name in have else 'absent'))

        if args.check:
            if 'escalated' in have:
                n = db.session.execute(text(
                    f'SELECT COUNT(*) FROM {TABLE} WHERE escalated = 1'
                )).scalar()
                twins = db.session.execute(text(
                    f"SELECT COUNT(*) FROM {TABLE} "
                    f"WHERE task_key LIKE '%.escalated'")).scalar()
                print(f'\n  rows flagged escalated: {n}')
                print(f'  twin rows still present: {twins}')
            print('\nCHECK ONLY — nothing written.')
            return

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            for name, _s, _p in COLUMNS:
                if name not in have:
                    continue
                try:
                    db.session.execute(text(
                        f'ALTER TABLE {TABLE} DROP COLUMN {name}'))
                    db.session.commit()
                    print(f'  dropped {name}')
                except Exception as exc:                  # noqa: BLE001
                    db.session.rollback()
                    print(f'  !! could not drop {name}: '
                          f'{type(exc).__name__}: {str(exc)[:150]}')
            return

        for name, sq, pg in COLUMNS:
            if name in have:
                print(f'  {name} already present.')
                continue
            db.session.execute(text(
                f'ALTER TABLE {TABLE} ADD COLUMN {name} '
                f'{sq if sqlite else pg}'))
            db.session.commit()
            print(f'  added {name}')


if __name__ == '__main__':
    main()
