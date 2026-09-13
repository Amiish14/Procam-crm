"""The default-password audit finds exactly the guessable accounts."""
import os
import sqlite3
import sys
import tempfile

from werkzeug.security import generate_password_hash

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, 'scripts'))

import audit_default_passwords as adp                          # noqa: E402
from data_quality_report import read_only_engine               # noqa: E402


def test_only_active_accounts_on_their_code_are_listed():
    path = os.path.join(tempfile.mkdtemp(), 'pw.db')
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE employees (emp_code TEXT, role TEXT, '
               'is_super_admin INTEGER, is_active INTEGER, password_hash TEXT)')
    for code, role, sup, active, pw in (
            ('DIR1', 'admin', 1, 1, 'dir1'),         # exposed super admin
            ('REP1', 'user', 0, 1, 'a-real-password'),
            ('REP2', 'user', 0, 1, 'rep2'),           # exposed
            ('OLD1', 'user', 0, 0, 'old1'),           # inactive: ignored
            ('PCM001', 'admin', 0, 1, 'admin@Procam25')):  # published
        db.execute('INSERT INTO employees VALUES (?,?,?,?,?)',
                   (code, role, sup, active, generate_password_hash(pw)))
    db.commit()
    db.close()
    with read_only_engine('sqlite:///' + path).connect() as conn:
        hits, total = adp.exposed(conn)
    assert total == 4
    assert {h['emp_code'] for h in hits} == {'DIR1', 'REP2', 'PCM001'}
    assert [h['why'] for h in hits if h['emp_code'] == 'PCM001'] == [
        'published in DEPLOY.md']
    assert [h for h in hits if h['is_super_admin']][0]['emp_code'] == 'DIR1'
