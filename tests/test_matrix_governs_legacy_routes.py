"""
Routes that used to ask "is the role called admin?" now ask the Access
Matrix. An administrator the matrix has narrowed loses what was taken
away; a person the matrix has granted gains it.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'MatrixLegacyTestOnly12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'matrixlegacy.db'))

from app import app as flask_app, db, Employee, Opportunity       # noqa: E402
from app.access.service import set_profile, ALL_PERMS              # noqa: E402
from app.models.access import DataScope                            # noqa: E402

flask_app.config['WTF_CSRF_ENABLED'] = False


@pytest.fixture()
def people():
    with flask_app.app_context():
        db.create_all()
        for code, role, vert in (('MLNARROW', 'admin', 'All'),
                                 ('MLGRANT', 'user', 'All'),
                                 ('MLHEAD', 'user', 'Warehousing'),
                                 ('MLREP', 'user', 'Warehousing'),
                                 ('MLOTHER', 'user', 'Installation')):
            e = Employee.query.filter_by(emp_code=code).first()
            if e is None:
                e = Employee(emp_code=code, name=code)
                db.session.add(e)
            e.role, e.vertical, e.is_active = role, vert, True
            e.must_change_pw, e.is_super_admin, e.session_version = \
                False, False, 0
        db.session.commit()
        # an admin by role, narrowed by the matrix to no admin rights
        set_profile('MLNARROW', DataScope.OWN, [], actor='SYSTEM')
        # a user by role, granted employee administration and triage
        set_profile('MLGRANT', DataScope.ALL,
                    ['admin.employees', 'admin.triage'], actor='SYSTEM')
        set_profile('MLHEAD', DataScope.VERTICAL, [], actor='SYSTEM')
        set_profile('MLREP', DataScope.OWN, [], actor='SYSTEM')
        set_profile('MLOTHER', DataScope.OWN, [], actor='SYSTEM')
        opp = Opportunity(opp_number='ML-1', owner_emp_code='MLREP',
                          stage='Proposal')
        db.session.add(opp)
        db.session.commit()
        oid = opp.id
    yield {'opp': oid}
    with flask_app.app_context():
        Opportunity.query.filter_by(id=oid).delete()
        Employee.query.filter_by(emp_code='MLNEW01').delete()
        db.session.commit()


def _c(code, role):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role=role, vertical='All', sv=0)
    return c


def test_a_narrowed_admin_loses_employee_administration(people):
    r = _c('MLNARROW', 'admin').post('/api/employees', json={
        'emp_code': 'MLNEW01', 'name': 'New'})
    assert r.status_code == 403 and r.get_json()['need'] == 'admin.employees'
    assert _c('MLNARROW', 'admin').get('/api/news').status_code == 403


def test_a_granted_user_gains_it(people):
    r = _c('MLGRANT', 'user').post('/api/employees', json={
        'emp_code': 'MLNEW01', 'name': 'New'})
    assert r.status_code == 200
    full = _c('MLGRANT', 'user').get('/api/employees').get_json()
    assert 'must_change_pw' in full[0]         # the full records, not names


def test_opportunity_edits_follow_data_scope(people):
    oid = people['opp']
    assert _c('MLHEAD', 'user').put(f'/api/opportunities/{oid}',
                                    json={'probability': 40}).status_code == 200
    assert _c('MLOTHER', 'user').put(f'/api/opportunities/{oid}',
                                     json={'probability': 90}).status_code == 403


def test_ownership_transfer_needs_triage_rights(people):
    oid = people['opp']
    r = _c('MLHEAD', 'user').put(f'/api/opportunities/{oid}',
                                 json={'owner_emp_code': 'MLHEAD'})
    assert r.status_code == 403
    r = _c('MLGRANT', 'user').put(f'/api/opportunities/{oid}',
                                  json={'owner_emp_code': 'MLHEAD'})
    assert r.status_code == 200


def test_email_administration_needs_the_email_permission(people):
    r = _c('MLNARROW', 'admin').get('/api/email/inbox')
    assert r.status_code == 403


def test_without_the_permission_the_admin_record_is_not_listed(people):
    """What a signed-in user without `admin.employees` gets back.

    This asserted that `email` was absent too, and it no longer is:
    CRM-WA-01 §3 added the work address to this projection on purpose,
    because narrowing it for the assignment dropdowns had removed the
    one field anybody needed in order to contact the PIC on a lead --
    an admin could see it and the sales team could not. The decision
    is written out at the endpoint.

    So the line this test holds is the ADMIN RECORD, not the address:
    no password state, no mobile number, no joining date, no active
    flag. The mobile is the one to watch -- nothing in the CRM shows a
    colleague's mobile in bulk, and the address was added without it
    deliberately.
    """
    rows = _c('MLREP', 'user').get('/api/employees').get_json()
    assert rows
    for field in ('must_change_pw', 'mobile', 'is_active', 'joined_on',
                  'is_super_admin', 'department', 'designation', 'id'):
        assert field not in rows[0], field
    for row in rows:
        # `email_hidden` only where the address is withheld, so the
        # lead screen can tell "no address on record" from "not yours
        # to see" -- two different answers that have to read
        # differently.
        assert set(row) <= {'emp_code', 'name', 'vertical', 'role',
                            'email', 'email_hidden'}, set(row)
        assert {'emp_code', 'name', 'email'} <= set(row)


def test_the_work_address_is_there_for_the_dropdowns(people):
    """The half of CRM-WA-01 §3 that the test above used to deny. If
    this is ever withdrawn it should be withdrawn on purpose, with
    this test changed by the same hand."""
    rows = _c('MLGRANT', 'user').get('/api/employees').get_json()
    assert 'email' in rows[0]
    assert any(r['email'] for r in rows), 'no address reached anybody'


# ── the address is scoped like /people/<code>, 19 Nov ────────────────
def _rows(code, role='user'):
    return {r['emp_code']: r
            for r in _c(code, role).get('/api/employees').get_json()}


def test_an_unrestricted_user_sees_every_address(people):
    """DataScope.ALL — `visible_codes()` is None, and nothing is
    withheld."""
    rows = _rows('MLGRANT')
    assert rows
    assert not any(r.get('email_hidden') for r in rows.values())


def test_a_user_with_own_scope_sees_no_colleagues_address(people):
    """This is the exposure that was found: every active employee's
    work address, in one call, to anybody signed in. `/people/<code>`
    refuses the same person with `p360.may_view`, so the two disagreed
    about who is reachable."""
    rows = _rows('MLREP')
    assert rows['MLOTHER']['email'] == ''
    assert rows['MLOTHER']['email_hidden'] is True
    assert rows['MLHEAD']['email'] == ''


def test_a_user_always_sees_their_own_address(people):
    """A picker that cannot show you your own address is a bug, not a
    precaution -- and it is the PIC's own address the lead screen
    prints for most people."""
    with flask_app.app_context():
        me = Employee.query.filter_by(emp_code='MLREP').first()
        me.email = 'mlrep@procamlogistics.com'
        db.session.commit()
    rows = _rows('MLREP')
    assert rows['MLREP']['email'] == 'mlrep@procamlogistics.com'
    assert 'email_hidden' not in rows['MLREP']


def test_a_vertical_head_sees_their_own_vertical_and_not_the_others(people):
    """DataScope.VERTICAL. MLHEAD and MLREP are both Warehousing;
    MLOTHER is Installation."""
    rows = _rows('MLHEAD')
    assert 'email_hidden' not in rows['MLREP'], (
        'a vertical head cannot reach their own vertical')
    assert rows['MLOTHER'].get('email_hidden') is True


def test_the_pickers_still_list_every_assignable_colleague(people):
    """What the scoping deliberately does NOT do. These rows are the
    owner and assignee dropdowns: a rep with OWN scope would otherwise
    find nobody but themselves in a reassignment dropdown. A code and
    a name are already on every lead row they can see; the address is
    what was over-shared, so the address is what is scoped."""
    rows = _rows('MLREP')
    for code in ('MLHEAD', 'MLOTHER', 'MLGRANT', 'MLNARROW'):
        assert code in rows, code
        assert rows[code]['name'], code


def test_an_administrator_is_unaffected(people):
    """`admin.employees` takes the full record, as it always did."""
    rows = _c('MLGRANT', 'user').get('/api/employees').get_json()
    assert 'must_change_pw' in rows[0]
    assert not any('email_hidden' in r for r in rows)


def test_an_inactive_colleague_is_not_listed_to_a_plain_user(people):
    """The non-admin projection filters on `is_active`, and it has to:
    it does not carry the flag, so a leaver would be indistinguishable
    from anybody else in an assignment dropdown."""
    with flask_app.app_context():
        gone = Employee.query.filter_by(emp_code='MLOTHER').first()
        gone.is_active = False
        db.session.commit()
    try:
        rows = _c('MLREP', 'user').get('/api/employees').get_json()
        assert 'MLOTHER' not in [r['emp_code'] for r in rows]
    finally:
        with flask_app.app_context():
            back = Employee.query.filter_by(emp_code='MLOTHER').first()
            back.is_active = True
            db.session.commit()


def test_subscription_management_needs_the_email_permission(people):
    assert _c('MLNARROW', 'admin').post(
        '/api/email/subscribe').status_code == 403


def test_two_employees_without_email_can_be_created(people):
    c = _c('MLGRANT', 'user')
    with flask_app.app_context():
        Employee.query.filter(Employee.emp_code.in_(['MLNOMAIL1',
                                                     'MLNOMAIL2'])).delete()
        db.session.commit()
    for code in ('MLNOMAIL1', 'MLNOMAIL2'):
        r = c.post('/api/employees', json={'emp_code': code, 'name': code})
        assert r.status_code == 200, r.get_data(as_text=True)[:200]
    with flask_app.app_context():
        Employee.query.filter(Employee.emp_code.in_(['MLNOMAIL1',
                                                     'MLNOMAIL2'])).delete()
        db.session.commit()


def test_presales_accounts_follow_the_matrix_not_the_role(people):
    from app import Company
    with flask_app.app_context():
        mine = Company(name='ML Warehouse Account', is_active=True,
                       pic_emp_code='MLREP')
        other = Company(name='ML Installation Account', is_active=True,
                        pic_emp_code='MLOTHER')
        db.session.add_all([mine, other])
        db.session.commit()
        ids = (mine.id, other.id)
    try:
        def seen(code, role):
            r = _c(code, role).get('/api/accounts')
            assert r.status_code == 200, r.get_data(as_text=True)[:200]
            return {a['id'] for a in r.get_json()['accounts']} & set(ids)
        # an administrator narrowed to Own sees neither
        assert seen('MLNARROW', 'admin') == set()
        # a vertical head sees the account of their vertical, not another
        assert seen('MLHEAD', 'user') == {ids[0]}
        assert seen('MLOTHER', 'user') == {ids[1]}
    finally:
        with flask_app.app_context():
            Company.query.filter(Company.id.in_(ids)).delete(
                synchronize_session=False)
            db.session.commit()
