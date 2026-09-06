"""The super admin owns access itself, so its guarantees need to hold
even when the matrix says otherwise.
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
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'SuperTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'super.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db, Employee = _main.app, _main.db, _main.Employee
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.models.access import AccessProfile, DataScope     # noqa: E402
from app.access.service import effective, SUPER_PERM, ALL_PERMS  # noqa: E402


@pytest.fixture(scope='module')
def cast():
    with flask_app.app_context():
        db.create_all()
        spec = (
            ('SUPER1', 'The Super',    'admin', 'All',             True),
            ('ADMIN1', 'Plain Admin',  'admin', 'All',             False),
            ('HEAD1',  'A Head',       'user',  'Heavy Transport', False),
        )
        for code, name, role, vert, sup in spec:
            e = Employee.query.filter_by(emp_code=code).first()
            if not e:
                e = Employee(emp_code=code, name=name)
                db.session.add(e)
            e.name, e.role, e.vertical = name, role, vert
            e.is_active, e.must_change_pw = True, False
            e.is_super_admin, e.is_vertical_head = sup, (code == 'HEAD1')
        db.session.commit()
    return True


def _c(code):
    c = flask_app.test_client()
    with flask_app.app_context():
        e = Employee.query.filter_by(emp_code=code).first()
        role, vert = e.role, e.vertical or ''
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role=role, vertical=vert)
    return c


# ── only the super admin owns the matrix ─────────────────────────────
def test_super_admin_can_open_the_matrix(cast):
    assert _c('SUPER1').get('/api/access/matrix').status_code == 200


def test_a_plain_admin_cannot_open_the_matrix(cast):
    """Other directors keep their own work but do not control access."""
    assert _c('ADMIN1').get('/api/access/matrix').status_code == 403


def test_a_vertical_head_cannot_open_the_matrix(cast):
    assert _c('HEAD1').get('/api/access/matrix').status_code == 403


def test_only_the_super_admin_can_write(cast):
    for who in ('ADMIN1', 'HEAD1'):
        r = _c(who).put('/api/access/profile/HEAD1',
                        data=json.dumps({'data_scope': 'all', 'perms': []}),
                        content_type='application/json')
        assert r.status_code == 403, f'{who} could rewrite access'


# ── the guarantees ───────────────────────────────────────────────────
def test_super_admin_sees_everything(cast):
    with flask_app.app_context():
        scope, perms = effective('SUPER1')
    assert scope == DataScope.ALL
    assert SUPER_PERM in perms
    assert set(ALL_PERMS).issubset(perms)


def test_a_stored_profile_cannot_demote_the_super_admin(cast):
    """Belt and braces: even a row written directly to the database loses
    to the column, so the owner cannot be locked out of their own portal."""
    with flask_app.app_context():
        db.session.add(AccessProfile(emp_code='SUPER1',
                                     data_scope=DataScope.OWN, perms=[]))
        db.session.commit()
        scope, perms = effective('SUPER1')
        assert scope == DataScope.ALL
        assert SUPER_PERM in perms
        db.session.delete(AccessProfile.query.filter_by(
            emp_code='SUPER1').first())
        db.session.commit()


def test_the_matrix_refuses_to_edit_the_super_admin(cast):
    r = _c('SUPER1').put('/api/access/profile/SUPER1',
                         data=json.dumps({'data_scope': 'own', 'perms': []}),
                         content_type='application/json')
    assert r.status_code == 400
    assert _c('SUPER1').get('/api/access/matrix').status_code == 200


def test_super_permission_is_not_a_checkbox(cast):
    """If it were grantable, anyone with the matrix could crown themselves."""
    assert SUPER_PERM not in ALL_PERMS

    assert _c('SUPER1').put('/api/access/profile/ADMIN1',
                            data=json.dumps({'data_scope': 'all',
                                             'perms': [SUPER_PERM]}),
                            content_type='application/json').status_code == 200
    with flask_app.app_context():
        _s, perms = effective('ADMIN1')
    assert SUPER_PERM not in perms
    assert _c('ADMIN1').get('/api/access/matrix').status_code == 403


def test_summary_explains_a_person_in_plain_english(cast):
    d = _c('SUPER1').get('/api/access/summary/HEAD1').get_json()
    assert d['ok']
    assert d['scope_label']
    assert isinstance(d['can_open'], list)


def test_summary_is_super_admin_only(cast):
    assert _c('ADMIN1').get('/api/access/summary/HEAD1').status_code == 403
