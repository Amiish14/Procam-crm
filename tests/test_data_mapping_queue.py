"""The §65 review queue — where uncertain links get a human decision.

Resolving must be safe in both directions: it links the records it says it
will, and it must not create a duplicate company that de-duplication would
immediately have to merge away again.
"""
import os
import sys
import json
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'QueueTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'queue.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Lead, Employee = _main.Company, _main.Lead, _main.Employee
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.models.data_mapping import (DataMappingQueue,        # noqa: E402
                                     MappingStatus)


@pytest.fixture()
def queued():
    with flask_app.app_context():
        db.create_all()
        DataMappingQueue.query.delete()
        Lead.query.delete()
        Company.query.delete()
        db.session.commit()

        for code, sup in (('QADMIN', True), ('QUSER', False)):
            e = Employee.query.filter_by(emp_code=code).first()
            if not e:
                e = Employee(emp_code=code, name=code)
                db.session.add(e)
            e.role = 'admin' if sup else 'user'
            e.is_active, e.must_change_pw = True, False
            e.is_super_admin, e.vertical = sup, 'All'
        db.session.commit()

        siemens = Company(name='Siemens Limited', is_active=True)
        db.session.add(siemens)
        db.session.flush()

        # Three leads, all carrying the same unmatched name.
        ids = []
        for n in range(3):
            lead = Lead(company='Siemens Gamesa', stage='New')
            db.session.add(lead)
            db.session.flush()
            ids.append(lead.id)
            db.session.add(DataMappingQueue(
                entity_type='Lead', entity_id=lead.id, field='company',
                raw_value='Siemens Gamesa', reason='no_match',
                candidates=[{'id': siemens.id, 'name': 'Siemens Limited',
                             'score': 80}],
                status=MappingStatus.PENDING))
        db.session.commit()
        return {'siemens': siemens.id, 'leads': ids}


def _c(code):
    c = flask_app.test_client()
    with flask_app.app_context():
        e = Employee.query.filter_by(emp_code=code).first()
        role = e.role
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role=role, vertical='All')
    return c


def _post(client, path, payload):
    return client.post(path, data=json.dumps(payload),
                       content_type='application/json')


def test_queue_groups_by_name(queued):
    """Three leads with one name is one decision, not three."""
    body = _c('QADMIN').get('/api/mapping/queue').get_json()
    assert body['pending'] == 3
    assert body['total_groups'] == 1
    group = body['groups'][0]
    assert group['count'] == 3
    assert group['raw_value'] == 'Siemens Gamesa'
    assert group['candidates'], 'near neighbours should be offered'


def test_resolving_links_every_record_in_the_group(queued):
    body = _c('QADMIN').get('/api/mapping/queue').get_json()
    ids = body['groups'][0]['ids']

    r = _post(_c('QADMIN'), '/api/mapping/resolve',
              {'ids': ids, 'company_id': queued['siemens']})
    assert r.status_code == 200
    assert r.get_json()['linked'] == 3

    with flask_app.app_context():
        for lead_id in queued['leads']:
            assert Lead.query.get(lead_id).company_id == queued['siemens']
        assert DataMappingQueue.query.filter_by(
            status=MappingStatus.PENDING).count() == 0


def test_creating_reuses_an_existing_company(queued):
    """Typing a name that already exists under another spelling must not
    create a second record — de-duplication would only have to undo it."""
    body = _c('QADMIN').get('/api/mapping/queue').get_json()
    ids = body['groups'][0]['ids']

    r = _post(_c('QADMIN'), '/api/mapping/resolve',
              {'ids': ids, 'create_name': 'Siemens Ltd.'})
    assert r.status_code == 200
    assert r.get_json()['company_id'] == queued['siemens'], \
        'a duplicate Company was created instead of reusing the match'

    with flask_app.app_context():
        assert Company.query.count() == 1


def test_creating_a_genuinely_new_company_works(queued):
    body = _c('QADMIN').get('/api/mapping/queue').get_json()
    ids = body['groups'][0]['ids']
    r = _post(_c('QADMIN'), '/api/mapping/resolve',
              {'ids': ids, 'create_name': 'Siemens Gamesa Renewable Energy'})
    assert r.status_code == 200
    with flask_app.app_context():
        assert Company.query.count() == 2
        new = Company.query.filter_by(
            name='Siemens Gamesa Renewable Energy').first()
        assert new is not None
        assert Lead.query.get(queued['leads'][0]).company_id == new.id


def test_skipping_leaves_records_unlinked_but_intact(queued):
    body = _c('QADMIN').get('/api/mapping/queue').get_json()
    ids = body['groups'][0]['ids']
    r = _post(_c('QADMIN'), '/api/mapping/skip', {'ids': ids})
    assert r.status_code == 200 and r.get_json()['skipped'] == 3
    with flask_app.app_context():
        for lead_id in queued['leads']:
            lead = Lead.query.get(lead_id)
            assert lead is not None, 'skipping must not delete anything'
            assert lead.company_id is None
            assert lead.company == 'Siemens Gamesa'


def test_resolving_twice_is_refused(queued):
    body = _c('QADMIN').get('/api/mapping/queue').get_json()
    ids = body['groups'][0]['ids']
    assert _post(_c('QADMIN'), '/api/mapping/resolve',
                 {'ids': ids, 'company_id': queued['siemens']}
                 ).status_code == 200
    again = _post(_c('QADMIN'), '/api/mapping/resolve',
                  {'ids': ids, 'company_id': queued['siemens']})
    assert again.status_code == 400


def test_a_normal_user_cannot_touch_the_queue(queued):
    assert _c('QUSER').get('/api/mapping/queue').status_code == 403
    r = _post(_c('QUSER'), '/api/mapping/resolve',
              {'ids': [1], 'company_id': queued['siemens']})
    assert r.status_code == 403
