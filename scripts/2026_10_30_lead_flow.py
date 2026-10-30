"""
CRM-LEAD-FLOW-01 — the tables the new lead flow needs.

    .venv/bin/python scripts/2026_10_30_lead_flow.py --check
    .venv/bin/python scripts/2026_10_30_lead_flow.py
    .venv/bin/python scripts/2026_10_30_lead_flow.py --down --yes

  crm_help_guides           the "why not, and what to do" catalogue
  crm_help_guide_shown      which guide was shown, and acted on
  leads.thread_key          the mail thread a lead belongs to
  leads.merged_into_id      the lead this one was merged into
  leads.sub_status          Contacted's detail (Call done, Visit…)
  leads.popup_seen_by       who has seen the new lead screen

All additive. The two tables are created from the models; the four
columns are nullable and existing rows are untouched.

--down drops the four columns and the two tables, and with them the
help wording an administrator has edited since. The seed catalogue
in `app/services/guided_help.py` is the floor, so the screens keep
explaining themselves either way.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect, text                      # noqa: E402

from app import app, db                                   # noqa: E402

COLUMNS = (
    ('leads', 'thread_key', 'VARCHAR(200)'),
    ('leads', 'merged_into_id', 'INTEGER'),
    ('leads', 'sub_status', 'VARCHAR(40)'),
    ('leads', 'popup_seen_by', 'BOOLEAN DEFAULT 0'),
)
TABLES = ('crm_help_guides', 'crm_help_guide_shown')


def _cols(table):
    try:
        return {c['name'] for c in inspect(db.engine).get_columns(table)}
    except Exception:
        return set()


def _tables():
    try:
        return set(inspect(db.engine).get_table_names())
    except Exception:
        return set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    import app.models.help_content as _help_models        # noqa: F401

    with app.app_context():
        print(f'database: {db.engine.url}')
        here = _tables()
        for name in TABLES:
            print(f'  {name}: '
                  + ('present' if name in here else 'absent'))
        for tbl, col, _d in COLUMNS:
            print(f'  {tbl}.{col}: '
                  + ('present' if col in _cols(tbl) else 'absent'))

        if args.check:
            if 'crm_help_guides' in here:
                n = db.session.execute(text(
                    'SELECT COUNT(*) FROM crm_help_guides')).scalar()
                print(f'\n  guides written: {n}')
            if 'thread_key' in _cols('leads'):
                n = db.session.execute(text(
                    'SELECT COUNT(*) FROM leads '
                    'WHERE thread_key IS NOT NULL')).scalar()
                print(f'  leads carrying a thread key: {n}')
            print('\nCHECK ONLY — nothing written.')
            return

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            for tbl, col, _d in reversed(COLUMNS):
                if col not in _cols(tbl):
                    print(f'  {tbl}.{col} already absent.')
                    continue
                # SQLite refuses to drop a column an index names,
                # and leaves the table unreadable if it gets that
                # far. The index goes first.
                for index in inspect(db.engine).get_indexes(tbl):
                    if col in (index.get('column_names') or []):
                        try:
                            db.session.execute(text(
                                f'DROP INDEX IF EXISTS '
                                f'{index["name"]}'))
                            db.session.commit()
                        except Exception:             # noqa: BLE001
                            db.session.rollback()
                try:
                    db.session.execute(text(
                        f'ALTER TABLE {tbl} DROP COLUMN {col}'))
                    db.session.commit()
                    print(f'  dropped {tbl}.{col}')
                except Exception as exc:              # noqa: BLE001
                    db.session.rollback()
                    print(f'  !! {tbl}.{col}: {str(exc)[:120]}')
            for name in reversed(TABLES):
                if name not in _tables():
                    print(f'  {name} already absent.')
                    continue
                try:
                    db.session.execute(text(f'DROP TABLE {name}'))
                    db.session.commit()
                    print(f'  dropped {name}')
                except Exception as exc:              # noqa: BLE001
                    db.session.rollback()
                    print(f'  !! {name}: {str(exc)[:120]}')
            return

        db.create_all()
        print('  tables created where missing')
        for tbl, col, ddl in COLUMNS:
            if col in _cols(tbl):
                print(f'  {tbl}.{col} already present.')
                continue
            db.session.execute(text(
                f'ALTER TABLE {tbl} ADD COLUMN {col} {ddl}'))
            db.session.commit()
            print(f'  added {tbl}.{col}')

        from app.services import guided_help
        added = guided_help.seed(actor='CRM-LEAD-FLOW-01')
        print(f'  help catalogue: {added} guide(s) added')


if __name__ == '__main__':
    main()
