"""
CRM-COCKPIT-02 — the columns and tables this release needs.

    .venv/bin/python scripts/2026_11_01_cockpit02.py --check
    .venv/bin/python scripts/2026_11_01_cockpit02.py
    .venv/bin/python scripts/2026_11_01_cockpit02.py --down --yes

  leads.quote_due_at          when our offer has to be in (§2)
  leads.quote_due_source      portal subject / mail body / user
  leads.quote_due_confirmed   has the owner agreed with it
  leads.quote_due_extensions  how many times it moved
  leads.quote_due_missed_at   passed with no quote recorded (§2.7)
  leads.last_activity_at      when anything last happened (§4.1)
  leads.last_activity_kind    what that last thing was
  leads.portal_key            RA / eRFQ / GeM / tender number (§5.6)
  leads.cargo_type …          what the mail already said (§5.1):
                              cargo type, qty, weight, dimensions,
                              special requirements, urgency, and
                              `auto_filled_json` — which of them the
                              CRM filled and nobody has confirmed
  lead_quote_due_history      every move of a due date (§2.4)
  crm_bulk_change_log         the reversible log (§8)

All additive: eight nullable columns and two new tables. No existing
row is touched and no default is back-filled here — the back-fills are
separate scripts, each writing to `crm_bulk_change_log` so each can be
undone on its own.

Run this before deploying the code: the application reads the new
columns, and a worker that restarts first would answer 500 on every
lead screen until the column exists.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect, text                      # noqa: E402

from app import app, db                                   # noqa: E402

COLUMNS = (
    ('leads', 'quote_due_at', 'DATETIME'),
    ('leads', 'quote_due_source', 'VARCHAR(20)'),
    ('leads', 'quote_due_confirmed', 'BOOLEAN'),
    ('leads', 'quote_due_extensions', 'INTEGER DEFAULT 0'),
    ('leads', 'quote_due_missed_at', 'DATETIME'),
    ('leads', 'last_activity_at', 'DATETIME'),
    ('leads', 'last_activity_kind', 'VARCHAR(20)'),
    ('leads', 'portal_key', 'VARCHAR(60)'),
    # §5.1 — the fields the mail already said, which until now only
    # existed inside `email_extracted_json`.
    ('leads', 'cargo_type', 'VARCHAR(120)'),
    ('leads', 'cargo_qty', 'VARCHAR(60)'),
    ('leads', 'cargo_weight_mt', 'FLOAT'),
    ('leads', 'cargo_dimensions', 'VARCHAR(200)'),
    ('leads', 'special_requirements', 'TEXT'),
    ('leads', 'urgency', 'VARCHAR(20)'),
    ('leads', 'auto_filled_json', 'TEXT'),
)
TABLES = ('lead_quote_due_history', 'crm_bulk_change_log')

#: The columns the new screens filter and sort on. Without these the
#: cockpit tiles are a full scan of 2,391 leads per tile per load.
INDEXES = (
    ('ix_leads_quote_due_at', 'leads', 'quote_due_at'),
    ('ix_leads_last_activity_at', 'leads', 'last_activity_at'),
    ('ix_leads_portal_key', 'leads', 'portal_key'),
)


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


def _indexes(table):
    try:
        return {i['name'] for i in inspect(db.engine).get_indexes(table)}
    except Exception:
        return set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    # `from ... import` and not `import app.models.cockpit02`: the
    # latter binds the name `app` in this module and shadows the Flask
    # app imported above, so the next `app.app_context()` fails.
    from app.models import cockpit02 as _models            # noqa: F401

    with app.app_context():
        print(f'database: {db.engine.url}')
        here, lead_cols = _tables(), _cols('leads')
        idx = _indexes('leads')
        for name in TABLES:
            print(f'  {name}: '
                  + ('present' if name in here else 'absent'))
        for _tbl, col, _d in COLUMNS:
            print(f'  leads.{col}: '
                  + ('present' if col in lead_cols else 'absent'))
        for name, _tbl, _col in INDEXES:
            print(f'  {name}: ' + ('present' if name in idx else 'absent'))

        if args.check:
            if 'quote_due_at' in lead_cols:
                for label, sql in (
                        ('leads with a quote due date',
                         'SELECT COUNT(*) FROM leads '
                         'WHERE quote_due_at IS NOT NULL'),
                        ('leads with a last-activity stamp',
                         'SELECT COUNT(*) FROM leads '
                         'WHERE last_activity_at IS NOT NULL'),
                        ('leads carrying a portal number',
                         'SELECT COUNT(*) FROM leads '
                         'WHERE portal_key IS NOT NULL')):
                    print(f'  {label}: '
                          f'{db.session.execute(text(sql)).scalar()}')
            if 'crm_bulk_change_log' in here:
                n = db.session.execute(text(
                    'SELECT COUNT(*) FROM crm_bulk_change_log')).scalar()
                print(f'  reversible log rows: {n}')
            print('\nCHECK ONLY — nothing written.')
            return

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            # The log table goes last: while it exists, every
            # back-fill this release ran can still be undone, and
            # dropping it is what makes them permanent.
            for name, _tbl, _col in INDEXES:
                try:
                    db.session.execute(text(f'DROP INDEX IF EXISTS {name}'))
                    db.session.commit()
                except Exception:                         # noqa: BLE001
                    db.session.rollback()
            for _tbl, col, _d in reversed(COLUMNS):
                if col not in _cols('leads'):
                    print(f'  leads.{col} already absent.')
                    continue
                try:
                    db.session.execute(text(
                        f'ALTER TABLE leads DROP COLUMN {col}'))
                    db.session.commit()
                    print(f'  dropped leads.{col}')
                except Exception as exc:                  # noqa: BLE001
                    db.session.rollback()
                    print(f'  !! leads.{col}: {str(exc)[:120]}')
            for name in reversed(TABLES):
                if name not in _tables():
                    print(f'  {name} already absent.')
                    continue
                try:
                    db.session.execute(text(f'DROP TABLE {name}'))
                    db.session.commit()
                    print(f'  dropped {name}')
                except Exception as exc:                  # noqa: BLE001
                    db.session.rollback()
                    print(f'  !! {name}: {str(exc)[:120]}')
            return

        db.create_all()
        print('  tables created where missing')
        for _tbl, col, ddl in COLUMNS:
            if col in _cols('leads'):
                print(f'  leads.{col} already present.')
                continue
            db.session.execute(text(
                f'ALTER TABLE leads ADD COLUMN {col} {ddl}'))
            db.session.commit()
            print(f'  added leads.{col}')
        for name, tbl, col in INDEXES:
            if name in _indexes(tbl):
                print(f'  {name} already present.')
                continue
            db.session.execute(text(
                f'CREATE INDEX {name} ON {tbl} ({col})'))
            db.session.commit()
            print(f'  created {name}')


if __name__ == '__main__':
    main()
