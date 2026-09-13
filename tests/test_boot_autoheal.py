"""
A restart before a migration must not take the CRM down.

A model column the database lacks breaks every query on that table. The
final audit added three — leads.vertical_confidence, leads.vertical_reason
and lead_notes.revisions — and a deploy that restarts the service before
running their scripts would have broken the lead list and the notes panel
until someone noticed. init_db's autoheal adds them at boot. This proves
it, against a database built WITHOUT them.
"""
import os
import sqlite3
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_boot_adds_the_columns_a_stale_database_is_missing():
    path = os.path.join(tempfile.mkdtemp(), 'stale.db')
    # First boot builds the full schema, then the three columns are
    # dropped to simulate a production database that predates them.
    env = dict(os.environ, DATABASE_URL='sqlite:///' + path,
               SECRET_KEY='test', ADMIN_INITIAL_PASSWORD='BootTestOnly12345',
               SESSION_COOKIE_SECURE='false')
    boot = [sys.executable, '-c', 'import app']
    subprocess.run(boot, cwd=_ROOT, env=env, check=True,
                   capture_output=True, timeout=120)

    db = sqlite3.connect(path)
    for table, col in (('leads', 'vertical_confidence'),
                       ('leads', 'vertical_reason'),
                       ('lead_notes', 'revisions')):
        db.execute(f'ALTER TABLE {table} DROP COLUMN {col}')
    db.commit()
    cols = {r[1] for r in db.execute('PRAGMA table_info(leads)')}
    assert 'vertical_confidence' not in cols
    db.close()

    # Second boot: the autoheal must put them back.
    subprocess.run(boot, cwd=_ROOT, env=env, check=True,
                   capture_output=True, timeout=120)
    db = sqlite3.connect(path)
    leads = {r[1] for r in db.execute('PRAGMA table_info(leads)')}
    notes = {r[1] for r in db.execute('PRAGMA table_info(lead_notes)')}
    db.close()
    assert {'vertical_confidence', 'vertical_reason'} <= leads
    assert 'revisions' in notes
