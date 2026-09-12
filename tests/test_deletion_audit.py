"""Deleting a lead must leave a record.

571 leads have been hard-deleted from production with no trace: the
dialog asked for a reason "for the audit trail", and that reason went
only to app.logger at INFO — a level the logger was discarding. Eight of
those leads were real business, one at Quoted stage, and nobody can say
who removed them or why.
"""
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'AuditTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'audit.db')

import importlib                                              # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Employee, Lead, LeadActivity = _main.Employee, _main.Lead, _main.LeadActivity
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.models.audit import DeletionAudit                    # noqa: E402


def _latest_audit(lead_id):
    """The most recent audit row for this id.

    SQLite hands a deleted row's id to the next insert, so entity_id on
    its own can name two different leads over time. The snapshot and the
    timestamp are what tell them apart.
    """
    return (DeletionAudit.query
            .filter_by(entity_type='Lead', entity_id=lead_id)
            .order_by(DeletionAudit.id.desc()).first())


@pytest.fixture(scope='module')
def client():
    with flask_app.app_context():
        db.create_all()
        e = Employee.query.filter_by(emp_code='DELADM').first()
        if not e:
            e = Employee(emp_code='DELADM', name='Delete Admin')
            db.session.add(e)
        e.role, e.is_active, e.must_change_pw = 'admin', True, False
        e.vertical, e.is_super_admin = 'All', True
        db.session.commit()
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='DELADM', name='Delete Admin', role='admin',
                 vertical='All')
    return c


def _lead(company='Doomed Ltd', **kw):
    with flask_app.app_context():
        lead = Lead(company=company, source='email',
                    stage=kw.pop('stage', 'New Opportunity'), **kw)
        db.session.add(lead)
        db.session.commit()
        return lead.id


def test_deleting_a_lead_writes_an_audit_row(client):
    lid = _lead('Audited Ltd', assigned_to='DELADM')
    r = client.delete(f'/api/leads/{lid}',
                      json={'reason': 'duplicate of 1234'})
    assert r.status_code == 200

    with flask_app.app_context():
        row = _latest_audit(lid)
        assert row is not None, 'the lead went with no audit record'
        assert row.performed_by == 'DELADM'
        assert row.reason == 'duplicate of 1234'
        assert row.action == 'delete'
        assert row.performed_at is not None


def test_the_audit_keeps_what_was_destroyed(client):
    """After a permanent delete the snapshot is all that remains, which
    is what makes an accidental deletion recoverable."""
    lid = _lead('Quoted Client Ltd', stage='Quoted', assigned_to='DELADM')
    client.delete(f'/api/leads/{lid}', json={'reason': 'oops'})

    with flask_app.app_context():
        row = _latest_audit(lid)
        assert row.snapshot.get('company') == 'Quoted Client Ltd'
        assert row.snapshot.get('stage') == 'Quoted'
        assert row.snapshot.get('assigned_to') == 'DELADM'


def test_the_audit_counts_what_went_with_it(client):
    lid = _lead('Cascade Ltd')
    with flask_app.app_context():
        db.session.add(LeadActivity(lead_id=lid, kind='call',
                                    subject='spoke to them'))
        db.session.commit()
    client.post(f'/api/leads/{lid}/notes', json={'note_text': 'a note'})
    client.delete(f'/api/leads/{lid}', json={'reason': 'cleanup'})

    with flask_app.app_context():
        row = _latest_audit(lid)
        assert row.linked.get('activities') == 1
        assert row.linked.get('notes') == 1


def test_a_delete_with_no_reason_is_still_recorded(client):
    """A missing reason is worth knowing about; losing the record is not."""
    lid = _lead('No Reason Ltd')
    assert client.delete(f'/api/leads/{lid}', json={}).status_code == 200
    with flask_app.app_context():
        assert _latest_audit(lid) is not None


def test_the_lead_really_is_gone_afterwards(client):
    lid = _lead('Gone Ltd')
    client.delete(f'/api/leads/{lid}', json={'reason': 'x'})
    with flask_app.app_context():
        assert db.session.get(Lead, lid) is None


def test_an_unauditable_delete_does_not_happen(monkeypatch, client):
    """If the record cannot be written, the lead stays. A silent
    unrecorded delete is the thing this exists to prevent."""
    lid = _lead('Protected Ltd')

    import app.models.audit as audit_mod

    class _Boom:
        def __init__(self, **kw):
            raise RuntimeError('audit table unavailable')
    monkeypatch.setattr(audit_mod, 'DeletionAudit', _Boom)

    r = client.delete(f'/api/leads/{lid}', json={'reason': 'should fail'})
    assert r.status_code == 500
    with flask_app.app_context():
        assert db.session.get(Lead, lid) is not None, \
            'the lead was deleted despite the audit failing'


def test_info_logging_is_not_discarded():
    """app.logger.info was being dropped, which is how the delete line —
    the only record there was — went nowhere for 571 leads."""
    import logging
    assert flask_app.logger.getEffectiveLevel() <= logging.INFO, \
        'app.logger.info is still being discarded'


def test_deleting_a_lead_leaves_no_history_for_the_next_one(client):
    """SQLite hands a deleted row's id to the next insert. Anything left
    behind attaches itself to whichever lead takes that id next — so a
    brand-new lead would open showing someone else's assignment history.
    """
    from app import LeadAssignmentHistory, LeadEmail, LeadNote
    lid = _lead('Cascade Check Ltd', assigned_to='DELADM')
    with flask_app.app_context():
        db.session.add(LeadAssignmentHistory(
            lead_id=lid, to_primary='DELADM', changed_by='DELADM'))
        db.session.add(LeadEmail(lead_id=lid, direction='inbound',
                                 body='an email'))
        db.session.add(LeadActivity(lead_id=lid, kind='call'))
        db.session.commit()

    client.delete(f'/api/leads/{lid}', json={'reason': 'cascade test'})

    with flask_app.app_context():
        for model in (LeadAssignmentHistory, LeadEmail, LeadNote,
                      LeadActivity):
            left = model.query.filter_by(lead_id=lid).count()
            assert left == 0, \
                f'{model.__name__} left {left} row(s) for the next lead'
