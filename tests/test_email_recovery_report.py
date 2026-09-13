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
      INSERT INTO leads VALUES (7,'G','email',
        'From: buyer@x.com Dear team, please quote for 2 cranes. Regards',
        'migrated_from_notes','<g>');
      INSERT INTO lead_notes VALUES (1,4,1);
      INSERT INTO lead_notes VALUES (2,1,0);
    """)
    db.commit()
    db.close()
    return create_engine('sqlite:///' + path)


def test_each_state_is_counted_once(engine):
    with engine.connect() as conn:
        c = report.counts(conn)
    assert c['email_leads'] == 6
    assert c['intact'] == 2            # ingested, and an unlabelled one
    assert c['restored'] == 1
    assert c['unrecoverable'] == 2     # everything the split flagged
    assert c['still_damaged'] == 1     # 'call summary'
    assert c['flagged_but_readable'] == 1
    assert c['email_lead_with_no_body'] == 1
    assert c['preserved_as_note'] == 1


def test_a_restored_lead_is_not_also_counted_intact(engine):
    """Otherwise recovery would inflate "intact" and hide how much of the
    history was ever damaged."""
    with engine.connect() as conn:
        c = report.counts(conn)
    assert c['intact'] + c['restored'] + c['unrecoverable'] + \
        c['email_lead_with_no_body'] == c['email_leads']


_MS = ('MS_TENANT_ID', 'MS_CLIENT_ID', 'MS_CLIENT_SECRET')
_GRAPH = ('GRAPH_TENANT_ID', 'GRAPH_CLIENT_ID', 'GRAPH_CLIENT_SECRET')


def test_the_blocker_is_named_not_implied(monkeypatch):
    for k in _MS + _GRAPH:
        monkeypatch.delenv(k, raising=False)
    assert 'not configured' in report.graph_blocker()
    for k in _MS:
        monkeypatch.setenv(k, 'x')
    assert '403' in report.graph_blocker()
    assert 'tenant-admin' in report.graph_blocker()


def test_the_blocker_reads_the_names_the_graph_client_reads(monkeypatch):
    """It used to check GRAPH_*, which nothing sets — so a correctly
    configured server was reported as having no credentials."""
    for k in _MS:
        monkeypatch.delenv(k, raising=False)
    for k in _GRAPH:
        monkeypatch.setenv(k, 'x')
    assert 'not configured' in report.graph_blocker()
    src = open(os.path.join(_ROOT, 'email_ingest', 'graph_client.py')).read()
    for k in _MS:
        assert k in src


def test_recovery_percentage(engine):
    with engine.connect() as conn:
        c = report.counts(conn)
    # one restored, one still damaged; the flagged lead whose text reads
    # as an email is not damage, exactly as the recovery script sees it
    assert c['damaged'] == 2
    assert c['recovery_pct'] == 50.0
    assert c['unrecoverable_no_message_id'] == 0
