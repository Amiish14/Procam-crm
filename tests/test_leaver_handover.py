"""Somebody has left: find their work, and make sure nothing picks them.

Two facts that were one. `employees.is_active` answers "can this
person sign in", and it is false for a suspension, a long leave, an
account made by mistake and somebody who has left -- so no screen or
report could tell a leaver from the rest, and Employee Master's
Activate button would put a departed person back into every assignment
list with one click. `employees.left_on` is the durable one.

What is NOT new is the enforcement: every candidate query in the CRM
already filters on `is_active`, and both assignment write paths refuse
an inactive person. These tests pin that down as well, because it is
what the leaver mark relies on -- a guard nobody tests is a guard that
quietly stops being there.

And one test that the handover script cannot reassign anything, which
is the part of the brief that was an instruction not to act.
"""
import ast
import importlib
import importlib.util
import os
import sys
import tempfile
from datetime import date, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'leaver-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Leaver-123456789')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'leaver.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Employee, Lead, Company = _main.Employee, _main.Lead, _main.Company

from app.models.audit import AuditEvent                      # noqa: E402
from app.services import leavers                             # noqa: E402

_spec = importlib.util.spec_from_file_location(
    'leaver_handover',
    os.path.join(_ROOT, 'scripts', '2026_11_19_leaver_handover.py'))
hand = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hand)

#: Invented people and an invented account: no real names or codes.
GONE = 'ZLV_OUT'
STAYS = 'ZLV_IN'
ADM = 'ZLV_ADM'
ALL = (GONE, STAYS, ADM)
TAG = 'ZLv '


@pytest.fixture(autouse=True)
def world():
    with flask_app.app_context():
        db.create_all()
        _purge()
        _emp(ADM, 'Admin Person', role='admin')
        _emp(GONE, 'Departing Person', role='user', vertical='Heavy Transport')
        _emp(STAYS, 'Remaining Person', role='user',
             vertical='Heavy Transport')
        account = Company(name=TAG + 'Client Works', is_active=True,
                          pic_emp_code=GONE, vertical='Heavy Transport')
        db.session.add(account)
        db.session.flush()
        db.session.add(Lead(
            company=TAG + 'Client Works', company_id=account.id,
            stage='Quoted', source='manual', assigned_to=GONE,
            assigned_name='Departing Person',
            estimated_value_inr=2_500_000,
            followup_date=date.today() - timedelta(days=3)))
        db.session.add(Lead(company=TAG + 'Closed Client', stage='Lost',
                            source='manual', assigned_to=GONE))
        db.session.commit()
        yield
        _purge()


def _purge():
    Lead.query.filter(Lead.company.like(TAG + '%')).delete(
        synchronize_session=False)
    Company.query.filter(Company.name.like(TAG + '%')).delete(
        synchronize_session=False)
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


def _token(client):
    r = client.get('/my-work')
    for header in r.headers.getlist('Set-Cookie'):
        if header.startswith('csrf_token='):
            return header.split('=', 1)[1].split(';')[0]
    return ''


def _mark(note='left the company'):
    with flask_app.app_context():
        ok, err = leavers.mark_left(GONE, actor=ADM, note=note,
                                    batch='LEFT-TEST-1')
        assert ok, err
        db.session.commit()


# ── the two facts are different ─────────────────────────────────────
def test_marking_someone_left_also_stops_the_login_and_the_sessions():
    with flask_app.app_context():
        before = Employee.query.filter_by(emp_code=GONE).first()
        was = before.session_version or 0
    _mark()
    with flask_app.app_context():
        emp = Employee.query.filter_by(emp_code=GONE).first()
        assert emp.left_on == date.today()
        assert emp.is_active is False, 'a leaver cannot sign in'
        assert (emp.session_version or 0) > was, \
            'sessions they still hold have to end now'


def test_an_inactive_person_is_not_by_itself_a_leaver():
    """The distinction the whole change exists for."""
    with flask_app.app_context():
        emp = Employee.query.filter_by(emp_code=STAYS).first()
        emp.is_active = False
        db.session.commit()
        assert leavers.has_left(STAYS) is False
        assert 'not an active employee' in leavers.why_not_assignable(STAYS)
    _mark()
    with flask_app.app_context():
        assert leavers.has_left(GONE) is True
        assert 'has left Procam' in leavers.why_not_assignable(GONE)


def test_the_payload_carries_both_so_a_screen_can_tell_them_apart():
    _mark()
    rows = {r['emp_code']: r for r in
            _client(ADM).get('/api/employees').get_json()}
    assert rows[GONE]['has_left'] is True
    assert rows[GONE]['left_on'] == str(date.today())
    assert rows[STAYS]['has_left'] is False


# ── and cannot be undone by accident ────────────────────────────────
def test_reactivating_a_leaver_is_refused():
    _mark()
    with flask_app.app_context():
        eid = Employee.query.filter_by(emp_code=GONE).first().id
    client = _client(ADM)
    r = client.put(f'/api/employees/{eid}', json={'is_active': True},
                   headers={'X-CSRFToken': _token(client)})
    assert r.status_code == 400
    said = r.get_json()['error']
    assert 'left Procam' in said and 'Clear that first' in said, said
    with flask_app.app_context():
        assert Employee.query.filter_by(emp_code=GONE).first().is_active \
            is False


def test_they_can_be_brought_back_by_saying_so_in_the_same_breath():
    _mark()
    with flask_app.app_context():
        eid = Employee.query.filter_by(emp_code=GONE).first().id
    client = _client(ADM)
    r = client.put(f'/api/employees/{eid}',
                   json={'is_active': True, 'left_on': ''},
                   headers={'X-CSRFToken': _token(client)})
    assert r.status_code == 200, r.get_data(as_text=True)
    with flask_app.app_context():
        emp = Employee.query.filter_by(emp_code=GONE).first()
        assert emp.is_active is True and emp.left_on is None


def test_the_undo_puts_the_active_flag_back_as_it_was():
    _mark()
    with flask_app.app_context():
        ok, err = leavers.unmark(GONE, actor=ADM, batch='LEFT-TEST-1')
        assert ok, err
        db.session.commit()
        emp = Employee.query.filter_by(emp_code=GONE).first()
        assert emp.left_on is None
        assert emp.is_active is True, 'they were active before the mark'


def test_marking_someone_left_is_audited_with_the_actor_and_the_reason():
    _mark(note='left Procam, confirmed')
    with flask_app.app_context():
        rows = AuditEvent.query.filter(
            AuditEvent.action == leavers.ACTION,
            AuditEvent.entity_id == GONE).all()
        assert rows, 'no audit row'
        assert rows[-1].actor == ADM
        assert 'confirmed' in (rows[-1].reason or '')
        assert rows[-1].new_value.get('batch') == 'LEFT-TEST-1'
        assert rows[-1].old_value.get('is_active') is True


# ── nothing may pick them ───────────────────────────────────────────
def test_no_lead_can_be_assigned_to_somebody_who_has_left():
    _mark()
    with flask_app.app_context():
        from app.services import lead_assignment
        said = lead_assignment.validate(GONE, '')
        assert said and 'has left Procam' in said


def test_an_account_pic_cannot_be_set_to_somebody_who_has_left():
    _mark()
    with flask_app.app_context():
        from app.directory import service as directory
        with pytest.raises(directory.DirectoryRefused):
            directory._active_employee(GONE, 'Primary PIC')


def test_the_bulk_employee_list_other_people_see_leaves_them_out():
    """Everybody without admin.employees gets the pickers' list, and it
    is active staff only -- which a leaver is not."""
    _mark()
    rows = {r['emp_code'] for r in
            _client(STAYS, role='user').get('/api/employees').get_json()}
    assert GONE not in rows
    assert STAYS in rows


def test_a_forwarded_mail_cannot_name_them_as_the_assignee():
    """mail_ingest.assignee_from_forward is how "X, please take this up"
    becomes an owner. It matches active employees only."""
    _mark()
    with flask_app.app_context():
        from app.services import mail_ingest
        emp, _why = mail_ingest.assignee_from_forward(
            'Departing, please take this up')
        assert emp is None or emp.emp_code != GONE


def test_the_assignment_dropdowns_are_built_from_the_assignable_list():
    """The server refuses a leaver, so an option offering one is a trap.
    The "All Team" FILTERS keep the whole list on purpose: filtering by
    a departed person is how their open work gets found."""
    import re
    src = open(os.path.join(_ROOT, 'templates', 'app.html'),
               encoding='utf-8').read()
    src = re.sub(r'/\*.*?\*/', ' ', src, flags=re.S)
    assert 'function ASSIGNABLE()' in src
    assert "em.is_active!==false && !em.has_left" in src
    # The filter list is the only EMPS.map left, and it marks a leaver.
    assert src.count('ASSIGNABLE().map(') >= 8, 'the pickers must use it'
    assert "em.has_left?' — left'" in src


# ── the report reads, and only reads ────────────────────────────────
def test_the_report_finds_the_open_work_with_what_it_is_worth():
    with flask_app.app_context():
        work = hand.leads_of(GONE) + hand.accounts_of(GONE)
    kinds = {(r['record_type'], r['their_role']) for r in work}
    assert ('Lead', 'PIC') in kinds
    assert ('Account', 'PIC') in kinds
    lead = [r for r in work if r['record_type'] == 'Lead'
            and r['stage'] == 'Quoted'][0]
    assert lead['open'] is True
    assert lead['value_inr'] == 2_500_000
    assert lead['customer'] == TAG + 'Client Works'
    closed = [r for r in work if r['stage'] == 'Lost'][0]
    assert closed['open'] is False, 'a lost lead is not open work'


def test_an_overdue_follow_up_is_listed_as_overdue():
    with flask_app.app_context():
        rows = hand.followups_of(GONE)
    assert rows and rows[0]['their_role'] == 'OVERDUE'


def test_the_config_that_still_names_them_is_reported_too():
    """The part that brings a departed name back after the handover."""
    with flask_app.app_context():
        emp = Employee.query.filter_by(emp_code=GONE).first()
        emp.is_vertical_head = True
        db.session.commit()
        rows = hand.config_rows(GONE, emp)
    assert any(r['what'] == 'Marked as a vertical head' for r in rows)


def test_the_report_cannot_reassign_anything():
    """Point 2 of the brief was an instruction NOT to act. A script that
    could reassign is one somebody runs with the wrong flag at the wrong
    moment, so it has no code that can."""
    src = open(os.path.join(_ROOT, 'scripts',
                            '2026_11_19_leaver_handover.py'),
               encoding='utf-8').read()
    tree = ast.parse(src)
    owner_fields = {'assigned_to', 'secondary_owner', 'pic_emp_code',
                    'secondary_pic_emp_code', 'backup_pic_emp_code',
                    'owner_emp_code', 'owner_user_id', 'prepared_by_id',
                    'lead_driver'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute):
                    assert target.attr not in owner_fields, \
                        f'this script writes {target.attr}'
    # And the only columns it writes at all are the leaver's own three,
    # which live in app/services/leavers.py, not here.
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute)}
    assert 'delete' not in called
    assert 'update' not in called


def test_a_dry_run_writes_nothing_to_the_database():
    with flask_app.app_context():
        before = Employee.query.filter_by(emp_code=GONE).first()
        was = (before.is_active, before.left_on)
        work = []
        for _name, finder in hand.GROUPS.items():
            work.extend(finder(GONE))
        emp = Employee.query.filter_by(emp_code=GONE).first()
        assert (emp.is_active, emp.left_on) == was
        assert work, 'and it found something, so the check means something'
