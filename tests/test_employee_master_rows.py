"""Employee Master: what the row says about the account, and editing it.

"Employee Master shows only Reset password and Activate on every row,
including active staff. Does Activate mean these accounts are
inactive?"

It can mean two different things, and that was the bug:

  the row really is deactivated -- `is_active` false, which stops the
  person signing in at all (app.py: the login query filters on it) and
  removes them from the team pickers; or

  nobody knows, because the payload never carried `is_active`.
  /api/employees answers a caller WITHOUT admin.employees with the
  pickers' narrow projection -- code, name, vertical, role, email, and
  no id, no state. The sidebar entry is gated on the ROLE instead, so
  an admin whose Access Matrix row does not tick Employee Master
  reached the screen, got that list, and saw `undefined?...:'Activate'`
  on every row -- active staff included -- with Reset password calling
  /api/employees/undefined.

So these tests pin the two projections apart, prove the screen is drawn
from the payload rather than from an assumption, and cover the Edit the
screen was missing. Nothing here asserts a colour or a label's wording.
"""
import importlib
import os
import re
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'empmaster-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'EmpMaster-123456789')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'empmaster.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Employee = _main.Employee

from app.models.audit import AuditEvent                       # noqa: E402

#: Invented people with invented codes: no employee names or codes from
#: the business in tests.
ADM = 'ZEM_ADM'
STAFF = 'ZEM_ONE'
GONE = 'ZEM_OFF'
ALL = (ADM, STAFF, GONE)


@pytest.fixture(autouse=True)
def world():
    with flask_app.app_context():
        db.create_all()
        _purge()
        _emp(ADM, 'Admin Person', role='admin')
        _emp(STAFF, 'Working Person', role='user',
             email='zem.one@procamgroup.in', designation='Executive',
             vertical='Project Freight')
        _emp(GONE, 'Departed Person', role='user', is_active=False)
        db.session.commit()
        yield
        _purge()


def _purge():
    AuditEvent.query.filter(AuditEvent.entity_id.in_(ALL)).delete(
        synchronize_session=False)
    Employee.query.filter(Employee.emp_code.in_(ALL)).delete(
        synchronize_session=False)
    db.session.commit()


def _emp(code, name, **kw):
    kw.setdefault('vertical', 'All')
    kw.setdefault('is_active', True)
    e = Employee(emp_code=code, name=name, industries='[]', **kw)
    e.set_password('x' * 20)
    db.session.add(e)
    return e


def _client(code, role='admin'):
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess['emp_code'] = code
        sess['role'] = role
    return client


def _rows(client):
    out = client.get('/api/employees').get_json()
    assert isinstance(out, list), out
    return {r['emp_code']: r for r in out if r.get('emp_code')}


# ── the two projections, which is what the screen reads ─────────────
def test_an_administrator_is_told_which_accounts_are_switched_off():
    rows = _rows(_client(ADM))
    assert rows[STAFF]['is_active'] is True
    assert rows[GONE]['is_active'] is False
    assert rows[GONE]['id'], 'without an id no row control can act'


def test_without_the_permission_the_state_is_absent_not_false():
    """The whole defect in one assertion. A missing field read as false
    is what printed "Activate" against people who are active."""
    rows = _rows(_client(STAFF, role='user'))
    assert STAFF in rows
    assert 'is_active' not in rows[STAFF]
    assert 'id' not in rows[STAFF]


def test_the_narrow_list_leaves_out_the_switched_off_people_entirely():
    """So it cannot be used as an employee master either: it is the
    picker's list, which is active staff only."""
    assert GONE not in _rows(_client(STAFF, role='user'))
    assert GONE in _rows(_client(ADM))


# ── the screen is drawn from the payload ────────────────────────────
APP_HTML = os.path.join(_ROOT, 'templates', 'app.html')


def _code_of(name):
    """A function's body out of app.html, with every comment removed.

    The comments here explain the guard, in the same words as the
    assertions. A test that reads them passes on the prose and not on
    the code, and then the comment is what gets edited.
    """
    src = open(APP_HTML, encoding='utf-8').read()
    start = src.index('async function ' + name)
    body = src[start:start + 6000]
    body = re.sub(r'/\*.*?\*/', ' ', body, flags=re.S)
    body = re.sub(r'<!--.*?-->', ' ', body, flags=re.S)
    body = re.sub(r'\{#.*?#\}', ' ', body, flags=re.S)
    return body


def test_the_row_decides_from_the_payload_whether_it_may_act():
    body = _code_of('rEmps')
    assert "'is_active' in emps[0]" in body, \
        'the screen must ask the payload, not assume'
    # Every control that needs an id sits behind that decision.
    guard = body.index('${full?')
    for call in ('openEmpEdit(', 'resetPw(', 'toggleEmp('):
        assert body.index(call) > guard, f'{call} is drawn unguarded'


def test_a_caller_without_the_permission_is_told_why_the_page_is_empty():
    body = _code_of('rEmps')
    said = body[:body.index('${full?')]
    assert 'Employee Master' in said and 'Access Matrix' in said, \
        'the read-only notice must name the permission that is missing'


def test_a_switched_off_account_says_so_on_the_row():
    """Rather than leaving it to be inferred from a button's verb."""
    body = _code_of('rEmps')
    assert 'cannot sign in' in body
    assert "emp.is_active?'Deactivate':'Activate'" in body


# ── the Edit the screen was missing ─────────────────────────────────
def _token(client):
    r = client.get('/my-work')
    for header in r.headers.getlist('Set-Cookie'):
        if header.startswith('csrf_token='):
            return header.split('=', 1)[1].split(';')[0]
    return ''


def _edit(client, eid, **body):
    return client.put(f'/api/employees/{eid}', json=body,
                      headers={'X-CSRFToken': _token(client)})


def _id_of(code):
    with flask_app.app_context():
        return Employee.query.filter_by(emp_code=code).first().id


def test_an_administrator_can_correct_the_three_fields_that_go_stale():
    eid = _id_of(STAFF)
    client = _client(ADM)
    r = _edit(client, eid, email='new.address@procamgroup.in',
              designation='Senior Executive', vertical='Warehousing')
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json().get('ok') is True
    with flask_app.app_context():
        emp = Employee.query.filter_by(emp_code=STAFF).first()
        assert emp.email == 'new.address@procamgroup.in'
        assert emp.designation == 'Senior Executive'
        assert emp.vertical == 'Warehousing'


def test_every_edit_leaves_a_row_saying_who_changed_what():
    eid = _id_of(STAFF)
    _edit(_client(ADM), eid, email='audited.one@procamgroup.in',
          designation='Manager', vertical='Heavy Transport')
    with flask_app.app_context():
        rows = AuditEvent.query.filter(
            AuditEvent.entity_id == STAFF,
            AuditEvent.action == 'employee.update').all()
        assert rows, 'an edit with no audit row is not an audited edit'
        changed = {}
        for ev in rows:
            changed.update(ev.new_value or {})
        assert changed.get('email') == 'audited.one@procamgroup.in'
        assert changed.get('designation') == 'Manager'
        assert changed.get('vertical') == 'Heavy Transport'
        assert any(ev.actor == ADM for ev in rows), 'and who did it'


def test_an_address_that_cannot_receive_mail_is_refused_at_the_form():
    """The made-up shape this week's script exists to remove must not
    be typeable back in through the screen."""
    eid = _id_of(STAFF)
    r = _edit(_client(ADM), eid, email='emp99@procamgroup.in')
    assert r.status_code == 400
    with flask_app.app_context():
        assert Employee.query.filter_by(emp_code=STAFF).first().email == \
            'zem.one@procamgroup.in', 'nothing may be written on a refusal'


def test_clearing_the_address_is_allowed_and_leaves_nothing_behind():
    """Empty is the honest value for somebody whose address nobody
    knows, and `employees.email` is unique, so it must be stored as
    nothing rather than as a blank."""
    eid = _id_of(STAFF)
    r = _edit(_client(ADM), eid, email='')
    assert r.status_code == 200, r.get_data(as_text=True)
    with flask_app.app_context():
        assert Employee.query.filter_by(emp_code=STAFF).first().email is None, \
            'a blank is a duplicate of the next blank; nothing is not'


def test_two_addresses_can_be_cleared_without_a_server_error():
    """The uniqueness constraint, which '' trips and NULL does not."""
    client = _client(ADM)
    for code in (STAFF, GONE):
        r = _edit(client, _id_of(code), email='')
        assert r.status_code == 200, (code, r.get_data(as_text=True))
    with flask_app.app_context():
        assert [e.email for e in Employee.query.filter(
            Employee.emp_code.in_((STAFF, GONE))).all()] == [None, None]


def test_somebody_without_the_permission_cannot_edit_anybody():
    r = _edit(_client(STAFF, role='user'), _id_of(STAFF),
              designation='Director')
    assert r.status_code in (403, 302)
    with flask_app.app_context():
        assert Employee.query.filter_by(
            emp_code=STAFF).first().designation == 'Executive'


def test_the_form_offers_the_vertical_the_person_already_has():
    """A select that does not contain the current value falls back to
    option zero, and saving then moves the person to a vertical nobody
    chose -- the defect the lead screen's owner select already had."""
    src = open(APP_HTML, encoding='utf-8').read()
    fn = src[src.index('function openEmpEdit'):]
    fn = fn[:fn.index('async function saveEmpEdit')]
    fn = re.sub(r'/\*.*?\*/', ' ', fn, flags=re.S)
    assert 'vs.unshift(emp.vertical)' in fn
    assert "'<option value=\"\">" in fn, 'and "none" has to be reachable'
