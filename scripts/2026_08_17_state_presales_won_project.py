"""
Additive schema migration for Phase P1 (CRM pre-sales / state / won→project).

Adds columns:
  companies.state           VARCHAR(80)
  opportunities.won_project_ref  VARCHAR(60)
  opportunities.won_project_at   TIMESTAMP

Idempotent — Postgres ADD COLUMN IF NOT EXISTS.

Run:
  cd /var/www/procam-crm
  sudo -u procamapp env PYTHONPATH=. venv/bin/python \\
      scripts/2026_08_17_state_presales_won_project.py
"""
from app import app, db
from sqlalchemy import text


COLS = [
    ('companies',     'state',            'VARCHAR(80)'),
    ('opportunities', 'won_project_ref',  'VARCHAR(60)'),
    ('opportunities', 'won_project_at',   'TIMESTAMP'),
]


def main():
    with app.app_context():
        print('\n=== CRM Phase P1 schema migration ===\n')
        for table, col, dtype in COLS:
            db.session.execute(text(
                f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {dtype}'))
            print(f'  ✓ ADD COLUMN IF NOT EXISTS {table}.{col} {dtype}')
        db.session.execute(text(
            'CREATE INDEX IF NOT EXISTS ix_companies_state ON companies(state)'))
        db.session.execute(text(
            'CREATE INDEX IF NOT EXISTS ix_opportunities_won_project '
            'ON opportunities(won_project_ref)'))
        db.session.commit()
        print('\n✓ Done.')


if __name__ == '__main__':
    main()
