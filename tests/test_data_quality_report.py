"""
The data-quality report lists the right rows and cannot write.

The schema is built by booting the real app into a scratch file (so the
columns are the ones production has), then seeded with plain SQL: one
clean record and one faulty record per report, so each report is held
to finding exactly the faulty one.
"""
import csv
import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import date, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, 'scripts'))

import data_quality_report as dq                               # noqa: E402


@pytest.fixture(scope='module')
def dbpath():
    path = os.path.join(tempfile.mkdtemp(), 'dq.db')
    env = dict(os.environ, DATABASE_URL='sqlite:///' + path,
               SECRET_KEY='test', ADMIN_INITIAL_PASSWORD='DqTestOnly1234567',
               SESSION_COOKIE_SECURE='false')
    subprocess.run([sys.executable, '-c', 'import app, app.models; '
                    'app.app.app_context().push(); app.db.create_all()'],
                   cwd=_ROOT, env=env, check=True, capture_output=True,
                   timeout=120)
    db = sqlite3.connect(path)
    x = db.execute
    x("DELETE FROM employees")
    x("INSERT INTO employees (emp_code, name, is_active, role) VALUES "
      "('GOOD', 'Good', 1, 'user'), ('GONE', 'Gone', 0, 'user')")
    past = (date.today() - timedelta(days=10)).isoformat()
    future = (date.today() + timedelta(days=10)).isoformat()
    # companies: 1 clean; 2 no owner, no vertical, no gstin, dup name of 3;
    # 3 inactive owner, same gstin as 4; 5 foreign, no gstin (not listed)
    x("INSERT INTO companies (id, name, pic_emp_code, vertical, gstin, country,"
      " is_active) VALUES "
      "(1, 'Clean Co', 'GOOD', 'Projects', '27AAAAA0000A1Z5', 'India', 1),"
      "(2, 'Acme Pvt Ltd', NULL, NULL, NULL, 'India', 1),"
      "(3, 'ACME Private Limited', 'GONE', 'Projects', '29BBBBB1111B1Z5', "
      "    'India', 1),"
      "(4, 'Other Name', 'GOOD', 'Projects', '29BBBBB1111B1Z5', 'India', 1),"
      "(5, 'Foreign GmbH', 'GOOD', 'Projects', NULL, 'Germany', 1)")
    x("INSERT INTO leads (id, company, company_id, assigned_to, stage, "
      "products, is_archived) VALUES "
      "(1, 'Clean Co', 1, 'GOOD', 'New', 'Project Freight', 0),"
      "(2, 'Acme', 2, NULL, 'New', NULL, 0),"
      "(3, 'Archived', 2, NULL, 'New', NULL, 1),"
      "(4, 'Foreign', 5, 'GOOD', 'New', 'Warehousing', 0),"
      "(5, 'Other', 4, 'GOOD', 'New', 'Chartering', 0)")
    x("INSERT INTO opportunities (id, opp_number, title, stage, owner_emp_code,"
      " company_id, expected_close_date, won_at) VALUES "
      f"(1, 'O1', 'open ok', 'Proposal', 'GOOD', 1, '{future}', NULL),"
      f"(2, 'O2', 'open late', 'Proposal', NULL, 1, '{past}', NULL),"
      f"(3, 'O3', 'won late but won', 'Won', 'GOOD', 1, '{past}', '{past}'),"
      f"(4, 'O4', 'won with po', 'Won', 'GOOD', 1, NULL, '{past}'),"
      f"(5, 'O5', 'won no po', 'Won', 'GOOD', 1, NULL, '{past}'),"
      # won, but nobody moved the stage — won_at is the truth
      f"(6, 'O6', 'won stage stale', 'Negotiation', 'GOOD', 1, '{past}',"
      f" '{past}')")
    x("INSERT INTO won_handovers (opportunity_id, account_id, po_ref, status) "
      "VALUES (4, 1, 'PO-1', 'Handover Pending'), "
      "(5, 1, NULL, 'Awaiting PO')")
    x("INSERT INTO contacts (id, name, email, phone, company_id, designation,"
      " is_active) VALUES "
      "(1, 'Ok', 'ok@x.com', '9876543210', 1, 'CEO', 1),"
      "(2, 'Dup', 'OK@x.com', NULL, 1, 'CFO', 1),"
      "(3, '', NULL, NULL, NULL, NULL, 1),"
      "(4, 'Phone dup', 'p@y.com', '+91 98765 43210', 1, 'COO', 1)")
    x("INSERT INTO lead_emails (lead_id, direction, intake_class, from_addr) "
      "VALUES (1, 'outbound', 'H_quote_submission', 'sales@procamgroup.in'),"
      "       (4, 'outbound', 'H_quote_submission', 'rates@agent.example'),"
      "       (5, 'inbound', 'H_quote_submission', 'rates@agent.example')")
    x("UPDATE leads SET stage = 'Quoted' WHERE id = 4")
    db.commit()
    db.close()
    return path


@pytest.fixture()
def conn(dbpath):
    with dq.read_only_engine('sqlite:///' + dbpath).connect() as c:
        yield c


def ids(report, conn):
    return sorted({r['id'] for r in dq.REPORTS[report](conn)[1]})


def test_owners(conn):
    assert ids('accounts_without_owner', conn) == [2, 3]
    problems = {r['id']: r['problem']
                for r in dq.accounts_without_owner(conn)[1]}
    assert problems[3] == 'owner is not an active employee'
    assert ids('leads_without_owner', conn) == [2]       # archived 3 excluded
    assert ids('opportunities_without_owner', conn) == [2]


def test_overdue_close_ignores_won(conn):
    rows = dq.opportunities_overdue_close(conn)[1]
    assert [r['id'] for r in rows] == [2]
    assert rows[0]['days_overdue'] == 10


def test_won_without_po_and_handover(conn):
    assert ids('won_without_handover', conn) == [3, 6]
    po = {r['id']: r['has_handover'] for r in dq.won_without_po(conn)[1]}
    assert po == {3: 'no', 5: 'yes', 6: 'no'}


def test_vertical_service_gstin(conn):
    assert ids('accounts_without_vertical', conn) == [2]
    assert ids('accounts_without_service', conn) == [2, 3]
    assert ids('accounts_missing_gstin', conn) == [2]    # foreign 5 excluded


def test_duplicates(conn):
    acc = dq.duplicate_accounts(conn)[1]
    assert {(r['match_type'], r['id']) for r in acc} == {
        ('name', 2), ('name', 3), ('gstin', 3), ('gstin', 4)}
    con = dq.duplicate_contacts(conn)[1]
    assert {(r['match_type'], r['id']) for r in con} == {
        ('email', 1), ('email', 2), ('phone', 1), ('phone', 4)}


def test_incomplete_contacts(conn):
    rows = dq.incomplete_contacts(conn)[1]
    assert [r['id'] for r in rows] == [3]
    assert 'no email or phone' in rows[0]['problems']


def test_run_writes_private_csvs_and_never_the_database(dbpath, conn):
    before = hashlib.sha256(open(dbpath, 'rb').read()).hexdigest()
    out = os.path.join(tempfile.mkdtemp(), 'dq')
    summary = dq.run(conn, out)
    assert len(summary) == len(dq.REPORTS) == 13
    assert hashlib.sha256(open(dbpath, 'rb').read()).hexdigest() == before
    path = os.path.join(out, 'leads_without_owner.csv')
    assert oct(os.stat(path).st_mode & 0o777) == '0o600'
    with open(path) as fh:
        assert [r['id'] for r in csv.DictReader(fh)] == ['2']


def test_the_engine_refuses_writes(conn):
    with pytest.raises(Exception, match='readonly'):
        conn.exec_driver_sql("UPDATE companies SET name = 'x'")


def test_quotes_received_filed_as_sent(conn):
    rows = dq.quotes_received_filed_as_sent(conn)[1]
    assert [(r['lead_id'], r['stage_possibly_wrong']) for r in rows] == [
        (4, 'yes')]


def test_the_report_shares_definitions_without_importing_the_app():
    """The thresholds and normalisation come from the live module's
    definitions file, and loading them must not import app.py — its boot
    autoheal is a write."""
    code = ('import sys; sys.path.insert(0, "scripts"); '
            'import data_quality_report as r; '
            'assert "app" not in sys.modules, sorted(sys.modules); '
            'assert r.defs.NO_CONTACT_DAYS > 0; '
            'assert r.CLOSED == r.defs.OPP_CLOSED; print("ok")')
    out = subprocess.run([sys.executable, '-c', code], cwd=_ROOT,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == 'ok'


def test_accounts_sharing_a_mail_domain_are_duplicates():
    """email_domains is stored as a JSON list. Split as plain text it read
    as one domain spelled '["acme.example"]', so accounts sharing a
    domain alongside another never matched."""
    from sqlalchemy import create_engine
    eng = create_engine('sqlite://')
    with eng.connect() as c:
        c.exec_driver_sql(
            'CREATE TABLE companies (id INTEGER, name TEXT, gstin TEXT, '
            'email_domains TEXT, pic_emp_code TEXT, vertical TEXT, '
            'is_active INTEGER)')
        c.exec_driver_sql(
            "INSERT INTO companies VALUES "
            "(1, 'North Works', NULL, '[\"acme.example\"]', NULL, NULL, 1),"
            "(2, 'South Works', NULL, '[\"acme.example\", \"b.example\"]',"
            " NULL, NULL, 1)")
        rows = dq.duplicate_accounts(c)[1]
    assert {(r['match_type'], r['match_key'], r['id']) for r in rows} == {
        ('email_domain', 'acme.example', 1),
        ('email_domain', 'acme.example', 2)}
