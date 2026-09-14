"""The lead commercial-values migration adds its columns to a database
that predates them, previews and records its backfill, and can undo it."""
import os
import sqlite3
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(_ROOT, 'scripts', '2026_10_05_lead_commercial_values.py')
COLS = ('value_currency', 'opportunity_value_num', 'opportunity_fx_rate',
        'value_basis', 'quote_no', 'quote_value_num', 'quote_fx_rate',
        'quote_date', 'quote_validity_date', 'quote_cost_num',
        'quote_revision', 'quote_revisions', 'quote_recorded_by',
        'quote_recorded_at')


def _run(path, *args):
    env = dict(os.environ, DATABASE_URL='sqlite:///' + path)
    return subprocess.run([sys.executable, SCRIPT, *args], cwd=_ROOT, env=env,
                          capture_output=True, text=True, timeout=120)


def _old_database():
    path = os.path.join(tempfile.mkdtemp(), 'old.db')
    env = dict(os.environ, DATABASE_URL='sqlite:///' + path, SECRET_KEY='t',
               ADMIN_INITIAL_PASSWORD='MigrationTest12345',
               SESSION_COOKIE_SECURE='false')
    subprocess.run([sys.executable, '-c', 'import app, app.models\nwith app.app.app_context(): app.db.create_all()'], cwd=_ROOT, env=env,
                   check=True, capture_output=True, timeout=180)
    db = sqlite3.connect(path)
    db.execute('DROP INDEX IF EXISTS ix_leads_quote_date')
    for c in COLS:
        db.execute(f'ALTER TABLE leads DROP COLUMN {c}')
    db.execute("DELETE FROM master_lists WHERE key = 'fx_rate'")
    if not db.execute('SELECT COUNT(*) FROM employees').fetchone()[0]:
        db.execute("INSERT INTO employees (emp_code, name, is_active) "
                   "VALUES ('MIG1', 'Migration', 1)")
    db.execute("INSERT INTO leads (company, stage, cost_million) VALUES ('Old M', 'New', 4.5)")
    db.execute("INSERT INTO leads (company, stage, cost_million, estimated_value_inr) "
               "VALUES ('Has rupees', 'New', 2, 3000000)")
    db.commit()
    db.close()
    return path


def _q(path, sql):
    db = sqlite3.connect(path)
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def test_check_then_apply_adds_columns_and_backfills_nothing():
    path = _old_database()
    before = open(path, 'rb').read()
    out = _run(path, '--check')
    assert out.returncode == 0, out.stderr[-600:]
    assert 'quote_date' in out.stdout and 'Nothing backfilled' in out.stdout
    assert open(path, 'rb').read() == before

    assert _run(path).returncode == 0
    cols = {r[1] for r in _q(path, 'PRAGMA table_info(leads)')}
    assert set(COLS) <= cols
    assert _q(path, "SELECT COUNT(*) FROM master_lists WHERE key='fx_rate'") == [(1,)]
    assert _q(path, "SELECT estimated_value_inr, cost_million FROM leads "
                    "WHERE company='Old M'") == [(None, 4.5)]
    assert 'already there' in _run(path, '--check').stdout
    assert _run(path, '--backfill').returncode != 0
