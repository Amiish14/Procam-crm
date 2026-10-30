"""
TKT-0079 — "Assign to" on the FF sales hub.

The API to stamp an enquiry with its customer reference has existed
since FF-COMMS-01 shipped; nothing on the page called it, so the
reference could only be issued by somebody with curl. This is the
control, wired to `api_assign_crn`, behind FEATURE_FF_CRM like the
rest of the hub.

"Assign to" is the business unit that will serve the enquiry —
that is what the named endpoint takes, and the reference is issued
under it. See QUESTIONS.md 27.
"""
import importlib
import json
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'ffassign-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'FfAssignTest-123456')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'ffassign.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
flask_app.config['WTF_CSRF_ENABLED'] = False
db = _main.db
Employee, Lead = _main.Employee, _main.Lead

FLAG = 'FEATURE_FF_CRM'
ADMIN = 'FFA_ADMIN'


def _flag(state):
    from app.models.integration import AppSetting
    from app.services import flags
    row = AppSetting.query.filter_by(key='flag.' + FLAG).first()
    if row is None:
        row = AppSetting(key='flag.' + FLAG)
        db.session.add(row)
    row.value_json = json.dumps(bool(state))
    db.session.commit()
    flags.cache_clear()


def _purge():
    from app.models.ff import CrnSequence
    Lead.query.filter(Lead.company.like('FFA %')).delete(
        synchronize_session=False)
    CrnSequence.query.filter(CrnSequence.bu == 'FFAT').delete(
        synchronize_session=False)
    Employee.query.filter_by(emp_code=ADMIN).delete(
        synchronize_session=False)
    _flag(False)
    db.session.commit()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        db.session.add(Employee(
            emp_code=ADMIN, name='FF Assign Director', password_hash='x',
            role='admin', is_active=True, vertical='Freight Forwarding',
            email='ffa@procamlogistics.com'))
        db.session.commit()
        _flag(True)
        yield
        _purge()


def _client():
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['emp_code'] = ADMIN
        s['role'] = 'admin'
    return c


def _lead(name='FFA Enquiry'):
    lead = Lead(company=name, stage='New Opportunity', source='manual',
                assigned_to=ADMIN, procam_vertical='Freight Forwarding')
    db.session.add(lead)
    db.session.commit()
    return lead


# ── the control exists, and only with the flag on ────────────────────
def test_the_page_404s_with_the_flag_off():
    with flask_app.app_context():
        _flag(False)
    assert _client().get('/ff').status_code == 404


def test_the_dropdown_is_on_the_page_with_the_flag_on():
    r = _client().get('/ff')
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'id="crnBu"' in html
    assert 'Assign to' in html
    assert 'crnGo()' in html


def test_the_dropdown_offers_the_default_unit():
    from app.services import crn as crn_svc
    with flask_app.app_context():
        default = crn_svc.default_bu()
    html = _client().get('/ff').get_data(as_text=True)
    assert f'value="{default}"' in html
    assert 'selected' in html


def test_the_dropdown_offers_units_already_in_the_register():
    """Read from the register, not from a list in the code: the
    pattern accepts any two-to-five letters on purpose."""
    with flask_app.app_context():
        from app.models.ff import CrnSequence
        db.session.add(CrnSequence(bu='FFAT', kind='C', fy=26,
                                   last_serial=3))
        db.session.commit()
    html = _client().get('/ff').get_data(as_text=True)
    assert 'value="FFAT"' in html


def test_the_units_always_include_the_default_even_when_empty():
    from app.ff.routes import business_units
    from app.services import crn as crn_svc
    with flask_app.app_context():
        assert crn_svc.default_bu() in business_units()


# ── it is wired to api_assign_crn ────────────────────────────────────
def test_assigning_stamps_the_reference():
    with flask_app.app_context():
        lead = _lead()
        lead_id = lead.id
    r = _client().post(f'/api/ff/leads/{lead_id}/crn',
                       json={'bu': 'FFAT'})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body['ok'] is True
    assert body['crn'].startswith('FFAT-C')
    assert body['already_had'] is False
    with flask_app.app_context():
        assert db.session.get(Lead, lead_id).business_unit == 'FFAT'


def test_a_second_assignment_keeps_the_first_reference():
    """A second reference would orphan the first thread, which is
    what the screen says in words before you press it."""
    with flask_app.app_context():
        lead = _lead()
        lead_id = lead.id
    first = _client().post(f'/api/ff/leads/{lead_id}/crn',
                           json={'bu': 'FFAT'}).get_json()
    again = _client().post(f'/api/ff/leads/{lead_id}/crn',
                           json={'bu': 'FFAT'}).get_json()
    assert again['ok'] is True
    assert again['crn'] == first['crn']
    assert again['already_had'] is True


def test_the_next_reference_can_be_read_without_taking_it():
    with flask_app.app_context():
        from app.models.ff import CrnSequence
        db.session.add(CrnSequence(bu='FFAT', kind='C', fy=26,
                                   last_serial=7))
        db.session.commit()
    data = _client().get('/api/ff/crn/next?bu=FFAT&kind=C').get_json()
    assert data['ok'] is True
    assert data['bu'] == 'FFAT'
    with flask_app.app_context():
        from app.models.ff import CrnSequence
        row = CrnSequence.query.filter_by(bu='FFAT', kind='C').first()
        assert row.last_serial == 7, 'peeking must not consume a number'


def test_assigning_404s_with_the_flag_off():
    with flask_app.app_context():
        lead = _lead()
        lead_id = lead.id
        _flag(False)
    r = _client().post(f'/api/ff/leads/{lead_id}/crn',
                       json={'bu': 'FFAT'})
    assert r.status_code == 404


def test_assigning_a_lead_outside_your_scope_is_not_found():
    with flask_app.app_context():
        lead = Lead(company='FFA Somebody Elses', stage='New Opportunity',
                    source='manual', assigned_to='NOT_FFA_ADMIN',
                    procam_vertical='Freight Forwarding')
        db.session.add(lead)
        db.session.commit()
        lead_id = lead.id
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['emp_code'] = 'FFA_NOBODY'
        s['role'] = 'user'
    assert c.post(f'/api/ff/leads/{lead_id}/crn',
                  json={'bu': 'FFAT'}).status_code in (401, 403, 404)
