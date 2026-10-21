"""Lead-entry rules decided on 21 Oct 2026 (B4, B6).

* A manual lead must carry what its vertical needs: a route for
  transport and freight, a site for warehousing and installation.
* Mail-created and imported leads are not refused.
* A Tender/GeM lead goes to the tender desk, whoever keys it in, and
  a non-admin may create one even though they cannot otherwise assign.
"""
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'RuleTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'rules.db')

from app import app as flask_app, db, Employee, Lead          # noqa: E402
from app.services import admin_settings, lead_rules           # noqa: E402

flask_app.config['WTF_CSRF_ENABLED'] = False

DESK_MAIL = 'tender.desk@example.com'


_CREATED = []


def _upsert(model, keys, **values):
    """Get-or-create, so the fixture works on the shared suite database.
    Rows this module creates are removed at the end of it."""
    row = model.query.filter_by(**keys).first()
    if row is None:
        row = model(**keys)
        db.session.add(row)
        _CREATED.append(row)
    for k, v in values.items():
        setattr(row, k, v)
    return row


def _cleanup(extra=()):
    for row in list(extra) + list(reversed(_CREATED)):
        try:
            db.session.delete(row)
        except Exception:
            pass
    db.session.commit()
    _CREATED.clear()


@pytest.fixture(scope='module')
def client():
    with flask_app.app_context():
        db.create_all()
        for code, mail in (('RULREP', 'rep@example.com'),
                           ('TDESK', DESK_MAIL)):
            _upsert(Employee, {'emp_code': code}, name=code, role='user',
                    email=mail, is_active=True, must_change_pw=False,
                    vertical='Heavy Transport')
        db.session.commit()
        admin_settings.save('tender_owner_email', DESK_MAIL, actor='test')
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='RULREP', name='RULREP', role='user',
                 vertical='Heavy Transport')
    yield c
    with flask_app.app_context():
        from app.models.integration import AppSetting
        from app.models.task_engine import TaskInstance
        made = [l.id for l in Lead.query.filter(Lead.company == 'Rule Co')]
        TaskInstance.query.filter(TaskInstance.entity_type == 'Lead',
                                  TaskInstance.entity_id.in_(made)).delete(
            synchronize_session=False)
        Lead.query.filter(Lead.id.in_(made)).delete(
            synchronize_session=False)
        AppSetting.query.filter_by(key='tender_owner_email').delete()
        db.session.commit()
        _cleanup()


def _post(c, **kw):
    body = {'company': 'Rule Co'}
    body.update(kw)
    return c.post('/api/leads', json=body)


def test_transport_lead_needs_origin_and_destination(client):
    r = _post(client, procam_vertical='Heavy Transport', project='Turbine')
    assert r.status_code == 400
    assert set(r.get_json()['missing']) == {'Origin', 'Destination'}


def test_complete_transport_lead_is_created_with_route(client):
    r = _post(client, procam_vertical='Heavy Transport', project='Turbine',
              origin='Mundra', destination='Raipur')
    assert r.status_code == 200, r.get_data(as_text=True)
    with flask_app.app_context():
        lead = db.session.get(Lead, r.get_json()['id'])
        assert (lead.origin, lead.destination) == ('Mundra', 'Raipur')


def test_warehouse_lead_needs_site_not_route(client):
    r = _post(client, procam_vertical='Warehousing', project='Storage')
    assert r.status_code == 400
    assert r.get_json()['missing'] == ['Site / warehouse location']
    r = _post(client, procam_vertical='Warehousing', project='Storage',
              site_location='Waluj')
    assert r.status_code == 200


def test_installation_lead_needs_site(client):
    r = _post(client, procam_vertical='Installation', project='Press')
    assert r.get_json()['missing'] == ['Site location']


def test_vertical_is_required_for_manual_lead(client):
    r = _post(client)
    assert r.status_code == 400
    assert 'Vertical' in r.get_json()['missing']


def test_imported_lead_is_not_refused(client):
    r = _post(client, source='import')
    assert r.status_code == 200


def test_tender_lead_goes_to_the_desk(client):
    r = _post(client, source=lead_rules.TENDER_SOURCE,
              procam_vertical='Installation', project='GeM bid',
              site_location='Dholera', assigned_to='RULREP')
    assert r.status_code == 200, r.get_data(as_text=True)
    with flask_app.app_context():
        lead = db.session.get(Lead, r.get_json()['id'])
        assert lead.assigned_to == 'TDESK'
        assert lead.source == lead_rules.TENDER_SOURCE


def test_tender_lead_stays_with_creator_when_desk_inactive(client):
    with flask_app.app_context():
        desk = Employee.query.filter_by(emp_code='TDESK').first()
        desk.is_active = False
        db.session.commit()
    try:
        r = _post(client, source=lead_rules.TENDER_SOURCE,
                  procam_vertical='Installation', project='GeM bid',
                  site_location='Dholera')
        assert r.status_code == 200
        with flask_app.app_context():
            assert db.session.get(Lead, r.get_json()['id']).assigned_to \
                == 'RULREP'
    finally:
        with flask_app.app_context():
            desk = Employee.query.filter_by(emp_code='TDESK').first()
            desk.is_active = True
            db.session.commit()


def test_non_admin_still_cannot_assign_to_someone_else(client):
    """The tender exception must not open general reassignment."""
    r = _post(client, procam_vertical='Installation', project='Press',
              site_location='Pune', assigned_to='TDESK')
    assert r.status_code == 403
