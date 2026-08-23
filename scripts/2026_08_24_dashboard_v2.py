"""CRM Dashboard v2 — additive schema migration.

Adds:
  * leads.lost_reason               (structured lost reason)
  * leads.stage_entered_at          (when current stage was entered — used
                                     by ageing buckets; back-filled to
                                     updated_at for legacy rows)
  * employees.is_vertical_head      (marks a Vertical Head account)
  * employees.vertical_head_id      (FK → employees.id, direct manager)
  * kpi_settings                    (configurable KPI master)
  * kpi_targets                     (per-scope target values per period)

Idempotent. Postgres + SQLite supported. Safe to re-run.

Run:
    cd /var/www/procam-crm
    env PYTHONPATH=. python scripts/2026_08_24_dashboard_v2.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import app, db
from sqlalchemy import text


ADD_COLS = [
    ("leads",     "lost_reason",         "VARCHAR(60)"),
    ("leads",     "stage_entered_at",    "TIMESTAMP"),
    ("employees", "is_vertical_head",    "BOOLEAN DEFAULT FALSE"),
    ("employees", "vertical_head_id",    "INTEGER"),
]

CREATE_KPI_SETTINGS = """
CREATE TABLE IF NOT EXISTS kpi_settings (
    id                  INTEGER PRIMARY KEY {AUTOINC},
    kpi_key             VARCHAR(60)  UNIQUE NOT NULL,
    name                VARCHAR(200) NOT NULL,
    category            VARCHAR(40)  NOT NULL,   -- activity / pipeline / conversion / commercial
    unit                VARCHAR(20)  DEFAULT 'count',   -- count / percent / inr
    source_expr         TEXT,                     -- python-side ref used by the KPI engine
    warning_threshold   NUMERIC(5,2) DEFAULT 80,
    success_threshold   NUMERIC(5,2) DEFAULT 100,
    default_weightage   NUMERIC(5,2) DEFAULT 10,
    is_active           BOOLEAN DEFAULT TRUE,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_KPI_TARGETS = """
CREATE TABLE IF NOT EXISTS kpi_targets (
    id                  INTEGER PRIMARY KEY {AUTOINC},
    kpi_key             VARCHAR(60)  NOT NULL,
    scope_type          VARCHAR(20)  NOT NULL,   -- company / vertical / team / user
    scope_key           VARCHAR(60),             -- vertical name, emp_code, etc.
    period_type         VARCHAR(20)  NOT NULL,   -- monthly / quarterly / fy / custom
    period_start        DATE         NOT NULL,
    period_end          DATE         NOT NULL,
    target_value        NUMERIC(15,2) NOT NULL,
    weightage           NUMERIC(5,2) DEFAULT 10,
    notes               TEXT,
    created_by          VARCHAR(20),
    updated_by          VARCHAR(20),
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

# Baseline KPI Master seeded so admin can start setting targets immediately.
DEFAULT_KPIS = [
    ('calls_done',       'Calls Done',              'activity',    'count'),
    ('profile_sent',     'Profiles Sent',           'activity',    'count'),
    ('appointments',     'Appointments',            'activity',    'count'),
    ('visits',           'Visits',                  'activity',    'count'),
    ('rfqs_generated',   'RFQs Generated',          'activity',    'count'),
    ('new_leads',        'New Leads',               'pipeline',    'count'),
    ('active_pipeline',  'Active Pipeline',         'pipeline',    'count'),
    ('opportunities',    'Opportunities',           'pipeline',    'count'),
    ('won_count',        'Deals Won',               'pipeline',    'count'),
    ('lost_count',       'Deals Lost',              'pipeline',    'count'),
    ('conversion_pct',   'Lead → Won Conversion %', 'conversion',  'percent'),
    ('rfq_won_pct',      'RFQ → Won %',             'conversion',  'percent'),
    ('won_value',        'Business Won (INR M)',    'commercial',  'inr'),
    ('pipeline_value',   'Pipeline Value (INR M)',  'commercial',  'inr'),
    ('followup_compliance', 'Follow-up Compliance %', 'activity',  'percent'),
]


def main():
    with app.app_context():
        dialect = db.engine.dialect.name
        autoinc = 'AUTOINCREMENT' if dialect == 'sqlite' else ''

        # ── Additive column adds ─────────────────────────────
        for tbl, col, dtype in ADD_COLS:
            if dialect == 'postgresql':
                sql = f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS {col} {dtype}"
            else:
                sql = f"ALTER TABLE {tbl} ADD COLUMN {col} {dtype}"
            try:
                db.session.execute(text(sql))
                db.session.commit()
                print(f'  [OK] {tbl}.{col}')
            except Exception as exc:
                db.session.rollback()
                msg = str(exc).lower()
                if 'duplicate' in msg or 'already exists' in msg:
                    print(f'  [skip] {tbl}.{col} present')
                else:
                    print(f'  [warn] {tbl}.{col}: {exc}')

        # ── KPI tables ───────────────────────────────────────
        for name, ddl in [('kpi_settings', CREATE_KPI_SETTINGS),
                          ('kpi_targets',  CREATE_KPI_TARGETS)]:
            try:
                db.session.execute(text(ddl.replace('{AUTOINC}', autoinc)))
                db.session.commit()
                print(f'  [OK] table {name}')
            except Exception as exc:
                db.session.rollback()
                print(f'  [warn] {name}: {exc}')

        # ── Seed KPI Master ──────────────────────────────────
        existing = {row[0] for row in db.session.execute(
            text('SELECT kpi_key FROM kpi_settings')).fetchall()}
        for key, name, cat, unit in DEFAULT_KPIS:
            if key in existing:
                continue
            db.session.execute(text(
                'INSERT INTO kpi_settings (kpi_key, name, category, unit, '
                'warning_threshold, success_threshold, default_weightage, '
                'is_active) VALUES (:k, :n, :c, :u, 80, 100, 10, TRUE)'
            ), {'k': key, 'n': name, 'c': cat, 'u': unit})
        db.session.commit()
        print(f'  seeded {len(DEFAULT_KPIS) - len(existing)} KPI defaults '
              f'({len(existing)} already present)')

        # ── Backfill stage_entered_at from updated_at for old rows ──
        try:
            n = db.session.execute(text(
                'UPDATE leads SET stage_entered_at = updated_at '
                'WHERE stage_entered_at IS NULL'
            )).rowcount
            db.session.commit()
            print(f'  backfilled {n} legacy stage_entered_at rows')
        except Exception as exc:
            db.session.rollback()
            print(f'  [warn] stage_entered_at backfill: {exc}')

        print('\nDone.')


if __name__ == '__main__':
    main()
