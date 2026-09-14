"""Indexes a model declares but an older table lacks are listed by
--check, created without --check, and the preflight then reports none
missing. Unique indexes are never created by the script."""
import os
import sqlite3
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(_ROOT, 'scripts', 'ensure_model_indexes.py')
sys.path.insert(0, os.path.join(_ROOT, 'scripts'))


def _run(path, *args):
    env = dict(os.environ, DATABASE_URL='sqlite:///' + path)
    return subprocess.run([sys.executable, SCRIPT, *args], cwd=_ROOT,
                          env=env, capture_output=True, text=True,
                          timeout=300)


def _database_missing_indexes():
    path = os.path.join(tempfile.mkdtemp(), 'old.db')
    env = dict(os.environ, DATABASE_URL='sqlite:///' + path,
               SECRET_KEY='test', ADMIN_INITIAL_PASSWORD='IndexTest123456',
               SESSION_COOKIE_SECURE='false')
    subprocess.run([sys.executable, '-c', 'import app'], cwd=_ROOT, env=env,
                   check=True, capture_output=True, timeout=180)
    db = sqlite3.connect(path)
    for ix in ('ix_companies_state', 'ix_companies_pic_emp_code',
               'ix_leads_assigned_to'):
        db.execute(f'DROP INDEX IF EXISTS {ix}')
    if not db.execute('SELECT COUNT(*) FROM employees').fetchone()[0]:
        db.execute("INSERT INTO employees (emp_code, name, is_active) "
                   "VALUES ('IDX1', 'Index', 1)")
    db.commit()
    db.close()
    return path


def _indexes(path):
    db = sqlite3.connect(path)
    try:
        return {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        db.close()


def test_check_lists_then_apply_creates_then_nothing_left():
    path = _database_missing_indexes()
    before = open(path, 'rb').read()
    out = _run(path, '--check')
    assert out.returncode == 0, out.stderr[-500:]
    assert 'WOULD create companies.ix_companies_state' in out.stdout
    assert 'companies.ix_companies_pic_emp_code' in out.stdout
    assert open(path, 'rb').read() == before

    applied = _run(path)
    assert applied.returncode == 0, applied.stderr[-500:]
    assert {'ix_companies_state', 'ix_companies_pic_emp_code',
            } <= _indexes(path)
    assert 'nothing to do' in _run(path, '--check').stdout

    import production_preflight as pf
    from data_quality_report import read_only_engine
    rep = pf.Report()
    with read_only_engine('sqlite:///' + path).connect() as conn:
        pf.check_schema(rep, conn, pf.expected_schema())
    idx = [r for r in rep.rows if r['check'] == 'indexes']
    assert idx and idx[0]['status'] == pf.PASS, idx


def test_a_unique_index_is_reported_not_created():
    path = _database_missing_indexes()
    db = sqlite3.connect(path)
    db.execute('DROP INDEX IF EXISTS ix_leads_email_message_id')
    db.commit()
    db.close()
    out = _run(path)
    assert out.returncode == 0, out.stderr[-500:]
    assert 'unique, not created: leads.ix_leads_email_message_id' in out.stdout
    assert 'ix_leads_email_message_id' not in _indexes(path)


def test_refuses_a_database_without_employees():
    path = os.path.join(tempfile.mkdtemp(), 'empty.db')
    sqlite3.connect(path).execute('CREATE TABLE employees (id INTEGER)')
    out = _run(path, '--check')
    assert out.returncode != 0
    assert 'Refusing to run' in (out.stdout + out.stderr)
