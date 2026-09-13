"""The release migration brings a pre-release database to exactly the
schema the code expects, is safe to repeat, and never runs on an empty
database."""
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(_ROOT, 'scripts', '2026_10_04_production_hardening.py')
sys.path.insert(0, os.path.join(_ROOT, 'scripts'))


def _run(path, *args):
    env = dict(os.environ, DATABASE_URL='sqlite:///' + path)
    return subprocess.run([sys.executable, SCRIPT, *args], cwd=_ROOT,
                          env=env, capture_output=True, text=True,
                          timeout=300)


def _old_database():
    """A database as production has it before this release."""
    path = os.path.join(tempfile.mkdtemp(), 'old.db')
    env = dict(os.environ, DATABASE_URL='sqlite:///' + path,
               SECRET_KEY='test', ADMIN_INITIAL_PASSWORD='MigrationTest12345',
               SESSION_COOKIE_SECURE='false')
    subprocess.run([sys.executable, '-c', 'import app'], cwd=_ROOT, env=env,
                   check=True, capture_output=True, timeout=180)
    db = sqlite3.connect(path)
    db.execute('DROP TABLE IF EXISTS audit_events')
    for col in ('session_version', 'failed_logins', 'locked_until',
                'temp_password_expires_at'):
        db.execute(f'ALTER TABLE employees DROP COLUMN {col}')
    db.execute('DROP INDEX IF EXISTS ix_leads_assigned_to')
    if not db.execute('SELECT COUNT(*) FROM employees').fetchone()[0]:
        db.execute("INSERT INTO employees (emp_code, name, is_active) "
                   "VALUES ('MIG1', 'Migration', 1)")
    db.commit()
    db.close()
    return path


def test_check_writes_nothing_then_apply_then_nothing_left():
    path = _old_database()
    before = open(path, 'rb').read()
    out = _run(path, '--check')
    assert out.returncode == 0, out.stderr[-500:]
    assert 'WOULD add table  audit_events' in out.stdout
    assert 'employees.session_version' in out.stdout
    assert 'ix_leads_assigned_to' in out.stdout
    assert open(path, 'rb').read() == before

    assert _run(path).returncode == 0
    again = _run(path, '--check')
    assert 'nothing to do' in again.stdout, again.stdout

    db = sqlite3.connect(path)
    cols = {r[1] for r in db.execute('PRAGMA table_info(employees)')}
    assert {'session_version', 'failed_logins', 'locked_until',
            'temp_password_expires_at'} <= cols
    audit_cols = {r[1] for r in db.execute('PRAGMA table_info(audit_events)')}
    assert {'actor', 'action', 'entity_type', 'old_value', 'new_value',
            'ip'} <= audit_cols
    db.close()


def test_the_created_schema_matches_the_models():
    path = _old_database()
    assert _run(path).returncode == 0
    import production_preflight as pf
    from data_quality_report import read_only_engine
    rep = pf.Report()
    with read_only_engine('sqlite:///' + path).connect() as conn:
        missing = pf.check_schema(rep, conn, pf.expected_schema())
    assert missing == []
    idx = [r for r in rep.rows if r['check'] == 'indexes'][0]
    assert 'audit_events' not in idx['detail']
    assert 'ix_leads_assigned_to' not in idx['detail']


def test_it_refuses_an_empty_database():
    path = os.path.join(tempfile.mkdtemp(), 'empty.db')
    sqlite3.connect(path).execute('CREATE TABLE employees (id INTEGER)')
    out = _run(path, '--check')
    assert out.returncode != 0 and 'Refusing' in (out.stdout + out.stderr)


def test_down_needs_yes_and_warns_about_the_audit_trail():
    path = _old_database()
    _run(path)
    out = _run(path, '--down')
    assert 'the audit trail' in out.stdout and 'nothing dropped' in out.stdout
    db = sqlite3.connect(path)
    assert db.execute("SELECT name FROM sqlite_master WHERE "
                      "name='audit_events'").fetchone()
    db.close()
