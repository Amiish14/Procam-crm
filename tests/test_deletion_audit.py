"""A lead is closed, never destroyed.

571 leads were hard-deleted with no trace: the dialog asked for a
reason "for the audit trail", and that reason went only to
app.logger at INFO, a level the logger was discarding. The audit
row this module was written to enforce fixed that.

It was not enough. By 21 October 830 leads were gone — 710 with a
snapshot, 120 with nothing at all, because the purge script wrote
no audit row of any kind. They came back only because a September
backup happened to exist.

So deletion is gone. The endpoint answers 410 and names what to
call instead; a lead that did not happen closes as Lost, with a
reason, and stays. These tests now hold that line.
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


def test_the_delete_endpoint_is_gone(client):
    lid = _lead('Audited Ltd', assigned_to='DELADM')
    r = client.delete(f'/api/leads/{lid}',
                      json={'reason': 'duplicate of 1234'})
    assert r.status_code == 410
    body = r.get_json()
    assert 'close-lost' in body['use']['close_as_lost']
    with flask_app.app_context():
        assert db.session.get(Lead, lid) is not None, \
            'the lead was destroyed by an endpoint that said it '\
            'would not'


def test_closing_as_lost_keeps_the_lead_and_records_why(client):
    lid = _lead('Quoted Client Ltd', stage='Quoted',
                assigned_to='DELADM')
    r = client.post(f'/api/leads/{lid}/close-lost',
                    json={'reason': 'went with the incumbent'})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    with flask_app.app_context():
        lead = db.session.get(Lead, lid)
        assert lead is not None
        assert lead.stage == 'Lost'
        assert lead.lost_reason == 'went with the incumbent'
        from app.models.audit import AuditEvent
        row = (AuditEvent.query
               .filter_by(entity_type='lead', entity_id=str(lid),
                          action='lead.closed_lost')
               .order_by(AuditEvent.id.desc()).first())
        assert row is not None
        assert 'went with the incumbent' in (row.reason or '')
        assert row.old_value.get('stage') == 'Quoted'


def test_closing_without_a_reason_is_refused(client):
    """A reason is the whole point: 120 leads have none, and nobody
    can say where they went."""
    lid = _lead('No Reason Ltd')
    for payload in ({}, {'reason': ''}, {'reason': '  '},
                    {'reason': 'x'}):
        r = client.post(f'/api/leads/{lid}/close-lost', json=payload)
        assert r.status_code == 400, payload
    with flask_app.app_context():
        assert db.session.get(Lead, lid).stage != 'Lost'


def test_a_duplicate_keeps_both_leads(client):
    kept = _lead('The Real One')
    dupe = _lead('The Same One Again')
    r = client.post(f'/api/leads/{dupe}/duplicate-of',
                    json={'duplicate_of': kept})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    with flask_app.app_context():
        a, b = db.session.get(Lead, dupe), db.session.get(Lead, kept)
        assert a is not None and b is not None
        assert a.stage == 'Lost'
        assert a.lost_reason == f'Others: duplicate of #{kept}'
        assert b.stage != 'Lost', 'the kept lead was closed too'


def test_a_duplicate_needs_a_lead_that_exists(client):
    lid = _lead('Lonely Ltd')
    assert client.post(f'/api/leads/{lid}/duplicate-of',
                       json={}).status_code == 400
    assert client.post(f'/api/leads/{lid}/duplicate-of',
                       json={'duplicate_of': lid}).status_code == 400
    assert client.post(f'/api/leads/{lid}/duplicate-of',
                       json={'duplicate_of': 99999}).status_code == 404
    with flask_app.app_context():
        assert db.session.get(Lead, lid).stage != 'Lost'


def test_closing_takes_the_open_tasks_with_it(client):
    """As any other close does — a task about a deal that stopped
    is not a task."""
    from app.models.task_engine import TaskInstance, TaskInstanceStatus

    lid = _lead('Tasked Ltd', assigned_to='DELADM')
    with flask_app.app_context():
        db.session.add(TaskInstance(
            task_key='del.test', entity_type='Lead', entity_id=lid,
            owner_user_id='DELADM',
            status=TaskInstanceStatus.PENDING))
        db.session.commit()
    client.post(f'/api/leads/{lid}/close-lost',
                json={'reason': 'budget pulled'})
    with flask_app.app_context():
        left = TaskInstance.query.filter(
            TaskInstance.entity_type == 'Lead',
            TaskInstance.entity_id == lid,
            TaskInstance.status.in_(TaskInstanceStatus.OPEN)).count()
        assert left == 0


def test_the_history_stays_with_the_lead(client):
    """Nothing cascades any more, because nothing is destroyed. The
    activity, the emails and the assignment history are the record
    of what happened and they stay attached to it."""
    from app import LeadAssignmentHistory, LeadEmail

    lid = _lead('Cascade Check Ltd', assigned_to='DELADM')
    with flask_app.app_context():
        db.session.add(LeadAssignmentHistory(
            lead_id=lid, to_primary='DELADM', changed_by='DELADM'))
        db.session.add(LeadEmail(lead_id=lid, direction='inbound',
                                 body='an email'))
        db.session.add(LeadActivity(lead_id=lid, kind='call'))
        db.session.commit()

    client.post(f'/api/leads/{lid}/close-lost',
                json={'reason': 'no longer required'})

    with flask_app.app_context():
        for model in (LeadAssignmentHistory, LeadEmail, LeadActivity):
            assert model.query.filter_by(lead_id=lid).count() == 1, \
                f'{model.__name__} was destroyed by a close'


def test_info_logging_is_not_discarded():
    """app.logger.info was being dropped, which is how the delete line —
    the only record there was — went nowhere for 571 leads."""
    import logging
    assert flask_app.logger.getEffectiveLevel() <= logging.INFO, \
        'app.logger.info is still being discarded'
