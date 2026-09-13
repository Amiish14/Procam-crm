"""
Every important business action leaves an audit event.

The trail is written by a session listener, so these tests change records
through the ordinary routes and services — not through the audit module —
and then look for the event. That is the property that matters: a path
that forgets to call the audit code is still audited.
"""
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'AuditTrailTestOnly12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'audit.db'))

from app import (app as flask_app, db, Employee, Lead, Company)  # noqa: E402
from app.models.audit import AuditEvent                          # noqa: E402
from app.services import audit                                   # noqa: E402

flask_app.config['WTF_CSRF_ENABLED'] = False
PASSWORD = 'AuditTrailPassword-1'


@pytest.fixture()
def people():
    with flask_app.app_context():
        db.create_all()
        for code, role, sup in (('ATADM', 'admin', True), ('ATREP', 'user',
                                                            False)):
            e = Employee.query.filter_by(emp_code=code).first()
            if e is None:
                e = Employee(emp_code=code, name=code)
                db.session.add(e)
            e.role, e.is_active, e.must_change_pw = role, True, False
            e.vertical, e.is_super_admin = 'All', sup
            e.session_version, e.failed_logins, e.locked_until = 0, 0, None
            e.set_password(PASSWORD)
        db.session.commit()
        ids = {c: Employee.query.filter_by(emp_code=c).first().id
               for c in ('ATADM', 'ATREP')}
        lead = Lead(company='Audit Trail Co', source='manual', stage='New',
                    assigned_to='ATREP')
        db.session.add(lead)
        db.session.commit()
        ids['lead'] = lead.id
        yield ids
        Lead.query.filter_by(id=lead.id).delete()
        db.session.commit()


def _c(code, role):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role=role, vertical='All')
    return c


def _events(action, entity_id=None):
    q = AuditEvent.query.filter_by(action=action)
    if entity_id is not None:
        q = q.filter_by(entity_id=str(entity_id))
    return q.order_by(AuditEvent.id.desc()).all()


def test_a_stage_change_through_the_api_is_audited(people):
    r = _c('ATADM', 'admin').put(f'/api/leads/{people["lead"]}',
                                 json={'stage': 'Call Done'},
                                 environ_base={'REMOTE_ADDR': '10.1.2.3'})
    assert r.status_code == 200
    with flask_app.app_context():
        ev = _events('lead.stage_change', people['lead'])[0]
        assert ev.old_value == {'stage': 'New'}
        assert ev.new_value == {'stage': 'Call Done'}
        assert ev.actor == 'ATADM' and ev.actor_role == 'admin'
        assert ev.ip == '10.1.2.3'


def test_a_reassignment_carries_its_reason(people):
    r = _c('ATADM', 'admin').put(
        f'/api/leads/{people["lead"]}',
        json={'assigned_to': 'ATADM',
              'reassignment_reason': 'Specialist required'})
    assert r.status_code == 200, r.get_json()
    with flask_app.app_context():
        ev = _events('lead.ownership_change', people['lead'])[0]
        assert ev.old_value['assigned_to'] == 'ATREP'
        assert ev.new_value['assigned_to'] == 'ATADM'
        assert ev.reason == 'Specialist required'


def test_saving_nothing_new_writes_nothing(people):
    with flask_app.app_context():
        before = AuditEvent.query.count()
        lead = db.session.get(Lead, people['lead'])
        lead.stage = lead.stage
        db.session.commit()
        assert AuditEvent.query.count() == before


def test_a_rolled_back_change_leaves_no_event(people):
    with flask_app.app_context():
        before = AuditEvent.query.count()
        lead = db.session.get(Lead, people['lead'])
        lead.stage = 'Lost'
        db.session.flush()
        db.session.rollback()
        assert AuditEvent.query.count() == before


def test_a_password_reset_is_audited_without_the_password(people):
    r = _c('ATADM', 'admin').put(f'/api/employees/{people["ATREP"]}',
                                 json={'reset_password': True})
    temp = r.get_json()['temp_password']
    with flask_app.app_context():
        ev = _events('employee.password_change', 'ATREP')[0]
        assert ev.reason == 'temporary password issued by an administrator'
        assert ev.new_value.get('sign_in_details') == 'changed'
        stored = json.dumps([ev.old_value, ev.new_value])
        assert temp not in stored
        emp = db.session.get(Employee, people['ATREP'])
        assert emp.password_hash not in stored


def test_role_and_activation_changes_are_audited(people):
    c = _c('ATADM', 'admin')
    c.put(f'/api/employees/{people["ATREP"]}', json={'role': 'presales'})
    c.delete(f'/api/employees/{people["ATREP"]}')
    with flask_app.app_context():
        role = _events('employee.role_change', 'ATREP')[0]
        assert (role.old_value, role.new_value) == ({'role': 'user'},
                                                    {'role': 'presales'})
        act = _events('employee.activation_change', 'ATREP')[0]
        assert act.new_value == {'is_active': False}


def test_access_matrix_changes_are_audited(people):
    from app.access.service import set_profile
    from app.models.access import DataScope
    with flask_app.test_request_context():
        from flask import session
        session['emp_code'] = 'ATADM'
        set_profile('ATREP', DataScope.OWN, ['module.rfq'], actor='ATADM')
        set_profile('ATREP', DataScope.VERTICAL, ['module.rfq',
                                                  'module.quotes'],
                    actor='ATADM')
        ev = _events('access.permission_change', 'ATREP')[0]
        assert ev.old_value['data_scope'] == DataScope.OWN
        assert ev.new_value['data_scope'] == DataScope.VERTICAL
        assert 'module.quotes' in ev.new_value['perms']


def test_account_ownership_changes_are_audited(people):
    with flask_app.app_context():
        co = Company(name='Audit Account Ltd', is_active=True,
                     pic_emp_code='ATREP')
        db.session.add(co)
        db.session.commit()
        created = _events('company.create', co.id)
        assert created and created[0].new_value['pic_emp_code'] == 'ATREP'
        co.pic_emp_code = 'ATADM'
        db.session.commit()
        ev = _events('company.ownership_change', co.id)[0]
        assert ev.new_value == {'pic_emp_code': 'ATADM'}
        db.session.delete(co)
        db.session.commit()
        assert _events('company.delete', co.id)


def test_sign_in_success_and_failure_are_audited(people):
    c = flask_app.test_client()
    c.post('/login', json={'emp_code': 'ATREP', 'password': 'wrong-one'})
    c.post('/login', json={'emp_code': 'ATREP', 'password': PASSWORD})
    with flask_app.app_context():
        fail = _events('auth.login_failure', 'ATREP')[0]
        assert fail.actor == 'ATREP'
        assert 'wrong-one' not in json.dumps([fail.old_value, fail.new_value])
        assert _events('auth.login_success', 'ATREP')


def test_bulk_delete_leaves_a_summary_and_one_event_per_lead(people):
    from app.bulk_admin import service as bulk
    import app.models.audit                                     # noqa: F401
    with flask_app.app_context():
        db.create_all()
        lead = Lead(company='Audit Bulk Gone', source='manual', stage='New')
        db.session.add(lead)
        db.session.commit()
        lid = lead.id
        bulk.delete([lid], 'duplicate import', 'ATADM')
        per_lead = _events('lead.delete', lid)[0]
        assert per_lead.reason == 'duplicate import'
        summary = _events('bulk.delete')[0]
        assert lid in summary.new_value['lead_ids']


def test_secrets_are_redacted_whatever_the_caller_passes():
    with flask_app.app_context():
        ev = audit.record('config.test', 'config', 'x',
                          new={'api_key': 'sk-live-123',
                               'nested': {'client_secret': 's3cr3t'},
                               'label': 'ok'},
                          commit=True)
        assert ev.new_value == {'api_key': audit.REDACTED,
                                'nested': {'client_secret': audit.REDACTED},
                                'label': 'ok'}


def test_changes_keeps_only_what_differs():
    old, new = audit.changes({'a': 1, 'b': 2}, {'a': 1, 'b': 3, 'c': 4})
    assert old == {'b': 2, 'c': None} and new == {'b': 3, 'c': 4}


# ── reading the trail ────────────────────────────────────────────────
def test_only_access_administrators_can_read_the_trail(people):
    assert _c('ATREP', 'user').get('/api/audit/events').status_code == 403
    assert _c('ATREP', 'user').get(
        '/api/audit/events.csv').status_code == 403
    r = _c('ATADM', 'admin').get('/api/audit/events')
    assert r.status_code == 200 and r.get_json()['ok'] is True


def test_the_trail_filters_by_action_record_and_person(people):
    c = _c('ATADM', 'admin')
    c.put(f'/api/leads/{people["lead"]}', json={'stage': 'Meeting'})
    got = c.get(f'/api/audit/events?action=lead.stage&entity_type=lead'
                f'&entity_id={people["lead"]}&actor=atadm').get_json()
    assert got['events']
    assert all(e['action'].startswith('lead.stage') and
               e['entity_id'] == str(people['lead']) and
               e['actor'] == 'ATADM' for e in got['events'])


def test_the_csv_cannot_carry_a_formula(people):
    from app.utils.spreadsheet_safe import safe_cell
    with flask_app.app_context():
        audit.record('lead.update', 'lead', 'x',
                     reason='=HYPERLINK("http://evil","click")', commit=True)
    r = _c('ATADM', 'admin').get('/api/audit/events.csv?entity_id=x')
    text = r.get_data(as_text=True)
    assert '\'=HYPERLINK' in text
    assert safe_cell('-1500') == '-1500'
    assert safe_cell('+91 98200 00000') == '+91 98200 00000'
    assert safe_cell('@SUM(A1)') == "'@SUM(A1)"
    with flask_app.app_context():
        assert _events('audit.export')


def test_the_trail_pages_backwards(people):
    with flask_app.app_context():
        for i in range(205):
            audit.record('test.page', 'page', str(i))
        db.session.commit()
    c = _c('ATADM', 'admin')
    first = c.get('/api/audit/events?action=test.page').get_json()
    assert len(first['events']) == 200 and first['next_before']
    rest = c.get(f'/api/audit/events?action=test.page'
                 f'&before={first["next_before"]}').get_json()
    assert len(rest['events']) >= 5
    assert not {e['id'] for e in first['events']} & \
        {e['id'] for e in rest['events']}
