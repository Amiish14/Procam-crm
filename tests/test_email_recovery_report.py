"""The Family A recovery report counts each state exactly once."""
import importlib.util
import os
import sqlite3
import tempfile

import pytest
from sqlalchemy import create_engine

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    'recovery_report', os.path.join(_ROOT, 'scripts',
                                    'email_recovery_report.py'))
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)


@pytest.fixture()
def engine():
    path = os.path.join(tempfile.mkdtemp(), 'r.db')
    db = sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE leads (id INTEGER PRIMARY KEY, company TEXT, source TEXT,
        original_email_body TEXT, original_email_source TEXT,
        email_message_id TEXT);
      CREATE TABLE lead_notes (id INTEGER PRIMARY KEY, lead_id INTEGER,
        migrated_from_legacy INTEGER);
      INSERT INTO leads VALUES (1,'A','email','Please quote','ingested','<a>');
      INSERT INTO leads VALUES (2,'B','email','Please quote',NULL,'<b>');
      INSERT INTO leads VALUES (3,'C','email','Recovered text',
                                'recovered_from_mailbox','<c>');
      INSERT INTO leads VALUES (4,'D','email','call summary',
                                'migrated_from_notes','<d>');
      INSERT INTO leads VALUES (5,'E','email','',NULL,'<e>');
      INSERT INTO leads VALUES (6,'F','manual',NULL,NULL,NULL);
      INSERT INTO lead_notes VALUES (1,4,1);
      INSERT INTO lead_notes VALUES (2,1,0);
    """)
    db.commit()
    db.close()
    return create_engine('sqlite:///' + path)


def test_each_state_is_counted_once(engine):
    with engine.connect() as conn:
        c = report.counts(conn)
    assert c['email_leads'] == 5
    assert c['intact'] == 2            # ingested, and an unlabelled one
    assert c['restored'] == 1
    assert c['unrecoverable'] == 1
    assert c['email_lead_with_no_body'] == 1
    assert c['preserved_as_note'] == 1


def test_a_restored_lead_is_not_also_counted_intact(engine):
    """Otherwise recovery would inflate "intact" and hide how much of the
    history was ever damaged."""
    with engine.connect() as conn:
        c = report.counts(conn)
    assert c['intact'] + c['restored'] + c['unrecoverable'] + \
        c['email_lead_with_no_body'] == c['email_leads']


def test_the_blocker_is_named_not_implied(monkeypatch):
    for k in ('GRAPH_TENANT_ID', 'GRAPH_CLIENT_ID', 'GRAPH_CLIENT_SECRET'):
        monkeypatch.delenv(k, raising=False)
    assert 'not configured' in report.graph_blocker()
    for k in ('GRAPH_TENANT_ID', 'GRAPH_CLIENT_ID', 'GRAPH_CLIENT_SECRET'):
        monkeypatch.setenv(k, 'x')
    assert '403' in report.graph_blocker()
    assert 'tenant-admin' in report.graph_blocker()
