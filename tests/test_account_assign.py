"""Account PIC assignment, one account or many.

Single and bulk go through the same presales.services.reassign_account,
both need the accounts.assign permission, and every change appends to
the account's PIC history. A bulk run reports each account, and one that
cannot be saved leaves the others saved.
"""
import json
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'AssignTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'assign.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Employee = _main.Company, _main.Employee
flask_app.config['WTF_CSRF_ENABLED'] = False

from presales.models import AccountAssignmentHistory      # noqa: E402
from app.models.access import AccessProfile, DataScope    # noqa: E402

ADMIN, REP, NEW, BACKUP, GONE = 'ASGADM', 'ASGREP', 'ASGNEW', 'ASGBAK', 'ASGOLD'


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        AccountAssignmentHistory.query.delete()
        Company.query.filter(Company.name.like('Assign %')).delete(
            synchronize_session=False)
        AccessProfile.query.filter(AccessProfile.emp_code.in_(
            [ADMIN, REP])).delete(synchronize_session=False)
        for code, role, active in ((ADMIN, 'admin', True), (REP, 'user', True),
                                   (NEW, 'user', True), (BACKUP, 'user', True),
                                   (GONE, 'user', False)):
            emp = Employee.query.filter_by(emp_code=code).first()
            if not emp:
                emp = Employee(emp_code=code, name=code.title())
                db.session.add(emp)
            emp.role, emp.is_active, emp.must_change_pw = role, active, False
            emp.is_super_admin = False
            emp.session_version = 0
        accts = [Company(name=f'Assign {n}', is_active=True, pic_emp_code=REP)
                 for n in range(4)]
        db.session.add_all(accts)
        db.session.commit()
        return [a.id for a in accts]


def _client(code, role):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role=role, vertical='All', sv=0)
    return c


def _post(client, url, body):
    return client.post(url, data=json.dumps(body),
                       content_type='application/json')


def _history(aid):
    with flask_app.app_context():
        return [(h.previous_pic_code, h.new_pic_code, h.assigned_by, h.reason)
                for h in AccountAssignmentHistory.query.filter_by(
                    account_id=aid).order_by(AccountAssignmentHistory.id)]


def _pics(aid):
    with flask_app.app_context():
        c = db.session.get(Company, aid)
        return c.pic_emp_code, c.secondary_pic_emp_code


def test_single_reassign_writes_history_and_secondary(world):
    aid = world[0]
    r = _post(_client(ADMIN, 'admin'), f'/api/accounts/{aid}/assign',
              {'new_pic': NEW, 'new_secondary': BACKUP, 'reason': 'territory'})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['changed'] is True
    assert _pics(aid) == (NEW, BACKUP)
    (prev, new, by, reason), = _history(aid)
    assert (prev, new, by) == (REP, NEW, ADMIN)
    assert reason.startswith('territory') and BACKUP in reason


def test_a_reassign_that_changes_nothing_adds_no_history(world):
    aid = world[0]
    r = _post(_client(ADMIN, 'admin'), f'/api/accounts/{aid}/assign',
              {'new_pic': REP, 'reason': 'same'})
    assert r.get_json()['changed'] is False
    assert _history(aid) == []


def test_an_inactive_or_unknown_pic_is_refused(world):
    admin = _client(ADMIN, 'admin')
    for code in (GONE, 'NOSUCH'):
        r = _post(admin, f'/api/accounts/{world[0]}/assign',
                  {'new_pic': code, 'reason': 'x'})
        assert r.status_code == 400 and 'not an active employee' in \
            r.get_json()['error']
    assert _pics(world[0]) == (REP, None)


def test_without_the_permission_neither_endpoint_assigns(world):
    with flask_app.app_context():
        db.session.add(AccessProfile(emp_code=REP, data_scope=DataScope.ALL,
                                     perms=['module.rfq']))
        db.session.commit()
    rep = _client(REP, 'user')
    r = _post(rep, f'/api/accounts/{world[0]}/assign',
              {'new_pic': NEW, 'reason': 'x'})
    assert r.status_code == 403 and r.get_json()['need'] == 'accounts.assign'
    r = _post(rep, '/api/accounts/assign-bulk',
              {'account_ids': world, 'new_pic': NEW, 'reason': 'x'})
    assert r.status_code == 403
    assert all(_pics(a)[0] == REP for a in world)
    detail = rep.get(f'/api/accounts/{world[0]}').get_json()
    assert detail['account']['can_assign'] is False


def test_bulk_assigns_each_account_and_writes_each_history(world):
    r = _post(_client(ADMIN, 'admin'), '/api/accounts/assign-bulk',
              {'account_ids': world[:3], 'new_pic': NEW, 'reason': 'reorg'})
    j = r.get_json()
    assert r.status_code == 200 and j['succeeded'] == 3 and j['failed'] == 0
    for aid in world[:3]:
        assert _pics(aid) == (NEW, None)
        assert _history(aid) == [(REP, NEW, ADMIN, 'reorg')]
    assert _pics(world[3]) == (REP, None)
    assert _history(world[3]) == []


def test_bulk_keeps_secondaries_unless_told_and_clears_when_told(world):
    admin = _client(ADMIN, 'admin')
    with flask_app.app_context():
        db.session.get(Company, world[0]).secondary_pic_emp_code = BACKUP
        db.session.commit()
    _post(admin, '/api/accounts/assign-bulk',
          {'account_ids': [world[0]], 'new_pic': NEW, 'reason': 'keep'})
    assert _pics(world[0]) == (NEW, BACKUP)
    _post(admin, '/api/accounts/assign-bulk',
          {'account_ids': [world[0]], 'new_pic': NEW, 'new_secondary': '',
           'reason': 'clear'})
    assert _pics(world[0]) == (NEW, None)


def test_one_failing_account_does_not_stop_or_undo_the_others(world):
    with flask_app.app_context():
        db.session.get(Company, world[1]).is_active = False
        # this one's secondary is the new PIC: the service refuses it
        db.session.get(Company, world[2]).secondary_pic_emp_code = NEW
        db.session.commit()
    r = _post(_client(ADMIN, 'admin'), '/api/accounts/assign-bulk',
              {'account_ids': world, 'new_pic': NEW, 'reason': 'mixed'})
    j = r.get_json()
    assert (j['succeeded'], j['failed']) == (2, 2), j
    by_id = {x['id']: x for x in j['results']}
    assert not by_id[world[1]]['ok'] and 'not found' in by_id[world[1]]['error']
    assert not by_id[world[2]]['ok'] and 'secondary' in by_id[world[2]]['error']
    assert _pics(world[0])[0] == NEW and _pics(world[3])[0] == NEW
    assert _pics(world[2]) == (REP, NEW) and _history(world[2]) == []


def test_bulk_needs_a_reason_and_a_valid_pic_and_a_bounded_batch(world):
    admin = _client(ADMIN, 'admin')
    r = _post(admin, '/api/accounts/assign-bulk',
              {'account_ids': world, 'new_pic': NEW, 'reason': '  '})
    assert r.status_code == 400 and 'reason' in r.get_json()['error']
    r = _post(admin, '/api/accounts/assign-bulk',
              {'account_ids': world, 'new_pic': GONE, 'reason': 'x'})
    assert r.status_code == 400
    r = _post(admin, '/api/accounts/assign-bulk',
              {'account_ids': list(range(1, 102)), 'new_pic': NEW,
               'reason': 'x'})
    assert r.status_code == 400
    assert all(_pics(a)[0] == REP for a in world)


def test_bulk_refuses_accounts_outside_the_callers_scope(world):
    with flask_app.app_context():
        db.session.add(AccessProfile(emp_code=REP, data_scope=DataScope.OWN,
                                     perms=['accounts.assign']))
        db.session.get(Company, world[3]).pic_emp_code = BACKUP
        db.session.commit()
    r = _post(_client(REP, 'user'), '/api/accounts/assign-bulk',
              {'account_ids': [world[0], world[3]], 'new_pic': NEW,
               'reason': 'handover'})
    by_id = {x['id']: x for x in r.get_json()['results']}
    assert by_id[world[0]]['ok']
    assert not by_id[world[3]]['ok'] and 'outside' in by_id[world[3]]['error']
    assert _pics(world[3])[0] == BACKUP


def test_the_permission_is_in_the_matrix_with_role_defaults():
    from app.access import service as acc
    assert 'accounts.assign' in acc.ALL_PERMS

    class Emp:
        is_super_admin = False
        role = 'user'
        is_vertical_head = False
    assert 'accounts.assign' not in acc.default_for(Emp())[1]
    Emp.is_vertical_head = True
    assert 'accounts.assign' in acc.default_for(Emp())[1]
    Emp.role, Emp.is_vertical_head = 'admin', False
    assert 'accounts.assign' in acc.default_for(Emp())[1]
