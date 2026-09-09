"""
Secondary lead assignee — WP5.

Every lead has one primary PIC who works it.  This adds an optional
secondary PIC: a monitor/backup who is notified on assignment and who is
expected to notice when the lead stops moving.

Changes
    leads.secondary_owner        VARCHAR(100)  (indexed, nullable)
    leads.secondary_owner_name   VARCHAR(100)  (nullable)
    lead_assignment_history      new table

Existing leads are untouched: no secondary is invented, and a lead with
only a primary owner behaves exactly as it did.

Usage
    python scripts/2026_09_18_secondary_assignee.py --check   # dry-run
    python scripts/2026_09_18_secondary_assignee.py           # apply
    python scripts/2026_09_18_secondary_assignee.py --down    # reverse

Back up first:
    cp procam_crm.db procam_crm.db.bak-$(date +%F-%H%M)

The down-migration drops the two columns and the history table, so it
discards any secondary assignment and audit trail recorded since the
migration ran — it reverses the schema, not the business decision. It
prints what will be lost and requires --yes to proceed.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, '.env'))
except ImportError:
    pass

from sqlalchemy import create_engine, text          # noqa: E402


COLUMNS = [
    ('secondary_owner', 'VARCHAR(100)'),
    ('secondary_owner_name', 'VARCHAR(100)'),
]

HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS lead_assignment_history (
    id              INTEGER PRIMARY KEY,
    lead_id         INTEGER NOT NULL REFERENCES leads(id),
    from_primary    VARCHAR(100),
    to_primary      VARCHAR(100),
    from_secondary  VARCHAR(100),
    to_secondary    VARCHAR(100),
    changed_at      DATETIME,
    changed_by      VARCHAR(20),
    note            VARCHAR(200)
)
"""


def _db_url():
    return os.environ.get('DATABASE_URL') or (
        'sqlite:///' + os.path.join(_ROOT, 'procam_crm.db'))


def _guard(conn):
    try:
        n = conn.execute(text('SELECT COUNT(*) FROM employees')).scalar()
    except Exception:
        n = 0
    if not n:
        raise SystemExit(
            'Refusing to run: no employees in this database, so it is not '
            'the production one. Check DATABASE_URL / .env.')
    return n


def _lead_columns(conn):
    rows = conn.execute(text('PRAGMA table_info(leads)')).fetchall()
    if not rows:
        raise SystemExit('Refusing to run: no leads table in this database.')
    return {r[1] for r in rows}


def _has_table(conn, name):
    return bool(conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"),
        {'n': name}).fetchone())


def up(conn, dry):
    have = _lead_columns(conn)
    to_add = [(c, t) for c, t in COLUMNS if c not in have]
    has_hist = _has_table(conn, 'lead_assignment_history')

    if dry:
        print('== DRY-RUN — nothing written ==')
        print(f'  WOULD add columns: '
              f'{", ".join(c for c, _ in to_add) or "(none — already there)"}')
        print(f'  WOULD create lead_assignment_history: '
              f'{"no — already exists" if has_hist else "yes"}')
        n = conn.execute(text('SELECT COUNT(*) FROM leads')).scalar()
        unassigned = conn.execute(text(
            "SELECT COUNT(*) FROM leads WHERE assigned_to IS NULL "
            "OR assigned_to = ''")).scalar()
        print(f'  leads: {n} ({unassigned} with no primary owner)')
        print('  existing leads are not modified.')
        return

    for col, coltype in to_add:
        conn.execute(text(f'ALTER TABLE leads ADD COLUMN {col} {coltype}'))
        print(f'  + leads.{col}')
    conn.execute(text('CREATE INDEX IF NOT EXISTS ix_leads_secondary_owner '
                      'ON leads (secondary_owner)'))
    conn.execute(text(HISTORY_DDL))
    conn.execute(text('CREATE INDEX IF NOT EXISTS '
                      'ix_lead_assignment_history_lead_id '
                      'ON lead_assignment_history (lead_id)'))
    print('  + lead_assignment_history')
    print('  existing leads unchanged.')


def down(conn, dry, confirmed):
    have = _lead_columns(conn)
    present = [c for c, _ in COLUMNS if c in have]
    hist_rows = 0
    if _has_table(conn, 'lead_assignment_history'):
        hist_rows = conn.execute(text(
            'SELECT COUNT(*) FROM lead_assignment_history')).scalar() or 0
    assigned = 0
    if 'secondary_owner' in have:
        assigned = conn.execute(text(
            "SELECT COUNT(*) FROM leads WHERE secondary_owner IS NOT NULL "
            "AND secondary_owner != ''")).scalar() or 0

    print('  This DISCARDS:')
    print(f'    {assigned} lead(s) with a secondary PIC recorded')
    print(f'    {hist_rows} assignment history row(s)')

    if dry:
        print('== DRY-RUN — nothing written ==')
        print(f'  WOULD drop columns: {", ".join(present) or "(none)"}')
        print('  WOULD drop table lead_assignment_history')
        return
    if not confirmed:
        raise SystemExit(
            '\n  Refusing to drop data without --yes. Re-run with --down '
            '--yes if that is genuinely what you want.')

    # The index must go first: SQLite refuses to drop a column while an
    # index still references it, and up() creates one on secondary_owner.
    conn.execute(text('DROP INDEX IF EXISTS ix_leads_secondary_owner'))
    for col in present:
        # SQLite has supported DROP COLUMN since 3.35 (2021).
        conn.execute(text(f'ALTER TABLE leads DROP COLUMN {col}'))
        print(f'  - leads.{col}')
    conn.execute(text('DROP TABLE IF EXISTS lead_assignment_history'))
    print('  - lead_assignment_history')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true', help='dry-run')
    ap.add_argument('--down', action='store_true',
                    help='reverse the migration (destructive)')
    ap.add_argument('--yes', action='store_true',
                    help='confirm a --down that discards data')
    args = ap.parse_args()

    url = _db_url()
    print(f'database: {url}')
    engine = create_engine(url)
    with engine.begin() as conn:
        print(f'  employees: {_guard(conn)}')
        if args.down:
            down(conn, args.check, args.yes)
        else:
            up(conn, args.check)
    print('  done.' if not args.check else '== end dry-run ==')


if __name__ == '__main__':
    main()
