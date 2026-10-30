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
    from app import LeadAssignmentHistory
    from app.models.ff import CrnSequence

    ours = [r.id for r in Lead.query.filter(
        Lead.company.like('FFA %')).all()]
    if ours:
        # Only this module's rows. Clearing the table outright wiped
        # history other modules had written, and the module that ran
        # next failed somewhere that looked unrelated.
        LeadAssignmentHistory.query.filter(
            LeadAssignmentHistory.lead_id.in_(ours)).delete(
                synchronize_session=False)
    Lead.query.filter(Lead.company.like('FFA %')).delete(
        synchronize_session=False)
    CrnSequence.query.filter(CrnSequence.bu == 'FFAT').delete(
        synchronize_session=False)
    Employee.query.filter(Employee.emp_code.like('FFA_%')).delete(
        synchronize_session=False)
    _flag(False)
    db.session.commit()
    # This module reads the vertical master, which caches on a
    # timer. Leaving it warm made the next module's vertical-head
    # permission fail on rows it had only just inserted.
    from app.services import vertical_heads
    vertical_heads.cache_clear()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        db.session.add(Employee(
            emp_code=ADMIN, name='FF Assign Director', password_hash='x',
            role='admin', is_active=True, vertical='Management',
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


# ── "Assign to" means a person (Q27, answered 30 Oct) ────────────────
#
# The first build read "Assign to" as the business unit, because
# that is what `api_assign_crn` takes. It means a person. Both
# controls exist now, as two dropdowns with two headings, because
# they answer two different questions about the same enquiry.
REP, GONE, OUTSIDE = 'FFA_REP', 'FFA_GONE', 'FFA_OUT'
FF_VERTICAL = 'Freight Forwarding'


def _person(code, name, *, vertical=FF_VERTICAL, active=True):
    db.session.add(Employee(
        emp_code=code, name=name, password_hash='x', role='user',
        vertical=vertical, is_active=active,
        email=code.lower() + '@procamlogistics.com'))
    db.session.commit()


def _ff_people():
    from app.ff.routes import people
    return people()


def test_the_people_come_from_the_employee_master():
    with flask_app.app_context():
        _person(REP, 'FFA Rep One')
        codes = [p['emp_code'] for p in _ff_people()]
        assert REP in codes


def test_somebody_who_has_left_is_not_offered():
    """`lead_assignment.validate` refuses them anyway. Offering a
    name that will be refused is worse than not offering it."""
    with flask_app.app_context():
        _person(GONE, 'FFA Departed', active=False)
        assert GONE not in [p['emp_code'] for p in _ff_people()]


def test_another_verticals_people_are_not_offered():
    with flask_app.app_context():
        _person(OUTSIDE, 'FFA Warehouse', vertical='Warehousing')
        assert OUTSIDE not in [p['emp_code'] for p in _ff_people()]


def test_the_dropdown_is_on_the_page_and_names_them():
    with flask_app.app_context():
        _person(REP, 'FFA Rep One')
    html = _client().get('/ff').get_data(as_text=True)
    assert 'id="ownWho"' in html
    assert 'FFA Rep One' in html
    assert 'ownGo()' in html


def test_the_two_dropdowns_are_separate_controls():
    """One sets who works it, the other which unit's reference it
    carries. Collapsing them was the original mistake."""
    with flask_app.app_context():
        _person(REP, 'FFA Rep One')
    html = _client().get('/ff').get_data(as_text=True)
    assert 'id="ownWho"' in html and 'id="crnBu"' in html
    assert '>Business unit</h3>' in html
    assert '>Assign to</h3>' in html


def test_an_empty_list_is_said_rather_than_widened():
    """No silent fallback to everybody: that hands a freight
    enquiry to a warehouse clerk."""
    html = _client().get('/ff').get_data(as_text=True)
    assert 'nobody to offer' in html


def test_assigning_sets_the_owner():
    with flask_app.app_context():
        _person(REP, 'FFA Rep One')
        lead = Lead(company='FFA Unowned', stage='New Opportunity',
                    source='manual', procam_vertical=FF_VERTICAL)
        db.session.add(lead)
        db.session.commit()
        lead_id = lead.id
    r = _client().post(f'/api/ff/leads/{lead_id}/owner',
                       json={'emp_code': REP})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()['ok'] is True
    with flask_app.app_context():
        assert db.session.get(Lead, lead_id).assigned_to == REP


def test_assigning_writes_the_history_row_with_actor_and_reason():
    with flask_app.app_context():
        _person(REP, 'FFA Rep One')
        lead = _lead('FFA Reassign')
        lead_id = lead.id
    _client().post(f'/api/ff/leads/{lead_id}/owner',
                   json={'emp_code': REP,
                         'reason': 'Different service vertical'})
    with flask_app.app_context():
        from app import LeadAssignmentHistory
        row = LeadAssignmentHistory.query.filter_by(
            lead_id=lead_id).one()
        assert row.to_primary == REP
        assert row.from_primary == ADMIN
        assert row.changed_by == ADMIN
        assert row.note == 'Different service vertical'


def test_replacing_an_owner_without_a_reason_is_refused():
    """The rule lives in `lead_assignment`, not in this screen —
    which is the point of going through it."""
    with flask_app.app_context():
        _person(REP, 'FFA Rep One')
        lead = _lead('FFA No Reason')
        lead_id = lead.id
    r = _client().post(f'/api/ff/leads/{lead_id}/owner',
                       json={'emp_code': REP})
    assert r.status_code == 400
    assert 'reason' in r.get_json()['error'].lower()
    with flask_app.app_context():
        assert db.session.get(Lead, lead_id).assigned_to == ADMIN


def test_assigning_somebody_who_has_left_is_refused():
    with flask_app.app_context():
        _person(GONE, 'FFA Departed', active=False)
        lead = Lead(company='FFA For Departed', stage='New Opportunity',
                    source='manual', procam_vertical=FF_VERTICAL)
        db.session.add(lead)
        db.session.commit()
        lead_id = lead.id
    r = _client().post(f'/api/ff/leads/{lead_id}/owner',
                       json={'emp_code': GONE})
    assert r.status_code == 400
    assert 'not an active employee' in r.get_json()['error']


def test_assigning_nobody_is_refused():
    with flask_app.app_context():
        lead = _lead('FFA Nobody')
        lead_id = lead.id
    r = _client().post(f'/api/ff/leads/{lead_id}/owner',
                       json={'emp_code': ''})
    assert r.status_code == 400


def test_assigning_the_same_person_again_changes_nothing():
    with flask_app.app_context():
        _person(REP, 'FFA Rep One')
        lead = Lead(company='FFA Idempotent', stage='New Opportunity',
                    source='manual', assigned_to=REP,
                    procam_vertical=FF_VERTICAL)
        db.session.add(lead)
        db.session.commit()
        lead_id = lead.id
    body = _client().post(f'/api/ff/leads/{lead_id}/owner',
                          json={'emp_code': REP}).get_json()
    assert body['ok'] is True and body['unchanged'] is True
    with flask_app.app_context():
        from app import LeadAssignmentHistory
        assert LeadAssignmentHistory.query.filter_by(
            lead_id=lead_id).count() == 0


def test_assigning_404s_with_the_flag_off():
    with flask_app.app_context():
        _person(REP, 'FFA Rep One')
        lead = _lead('FFA Flagged Off')
        lead_id = lead.id
        _flag(False)
    r = _client().post(f'/api/ff/leads/{lead_id}/owner',
                       json={'emp_code': REP})
    assert r.status_code == 404
