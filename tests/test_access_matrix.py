"""The Access Control matrix decides everything, so it is worth testing hard.

Covers: only admins can read or change it, changes actually take effect on
reports and modules, data_scope narrows what a person sees, and an admin
cannot lock themselves out of the screen that would undo a mistake.
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
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'MatrixTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'matrix.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db, Employee = _main.app, _main.db, _main.Employee
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.models.access import AccessProfile, DataScope     # noqa: E402
from app.access.service import effective, ALL_PERMS        # noqa: E402


@pytest.fixture(scope='module')
def people():
    with flask_app.app_context():
        db.create_all()
        for code, name, role, vert in (
                ('MADMIN', 'Matrix Admin', 'admin', 'All'),
                ('MHEAD',  'Matrix Head',  'user',  'Heavy Transport'),
                ('MREP',   'Matrix Rep',   'user',  'Heavy Transport')):
            e = Employee.query.filter_by(emp_code=code).first()
            if not e:
                e = Employee(emp_code=code, name=name)
                db.session.add(e)
            e.name, e.role, e.vertical = name, role, vert
            e.is_active, e.is_vertical_head = True, False
            e.must_change_pw = False
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


def _put(client, code, payload):
    return client.put('/api/access/profile/' + code,
                      data=json.dumps(payload),
                      content_type='application/json')


# ── who may touch the matrix ─────────────────────────────────────────
def test_admin_can_read_matrix(people):
    r = _c('MADMIN').get('/api/access/matrix')
    assert r.status_code == 200
    d = r.get_json()
    assert d['ok'] and d['people'] and d['groups'] and d['presets']


def test_non_admin_cannot_read_matrix(people):
    assert _c('MREP').get('/api/access/matrix').status_code == 403


def test_non_admin_cannot_grant_themselves(people):
    """If this ever passes, every other permission is decorative."""
    r = _put(_c('MREP'), 'MREP',
             {'data_scope': 'all', 'perms': list(ALL_PERMS)})
    assert r.status_code == 403
    with flask_app.app_context():
        scope, perms = effective('MREP')
    assert scope != DataScope.ALL
    assert 'admin.access' not in perms


# ── changes take effect ──────────────────────────────────────────────
def test_granting_a_report_opens_it(people):
    rep = _c('MREP')
    assert rep.get('/reports/open-tasks').status_code == 403

    assert _put(_c('MADMIN'), 'MREP',
                {'data_scope': 'vertical',
                 'perms': ['reports.action']}).status_code == 200

    assert _c('MREP').get('/reports/open-tasks').status_code == 200
    # a category they were not granted stays shut
    assert _c('MREP').get('/reports/price-comparison').status_code == 403


def test_revoking_closes_it_again(people):
    assert _put(_c('MADMIN'), 'MREP',
                {'data_scope': 'own', 'perms': []}).status_code == 200
    assert _c('MREP').get('/reports/open-tasks').status_code == 403


def test_module_permission_gates_a_blueprint(people):
    assert _put(_c('MADMIN'), 'MREP',
                {'data_scope': 'own', 'perms': []}).status_code == 200
    assert _c('MREP').get('/quotes').status_code == 403

    assert _put(_c('MADMIN'), 'MREP',
                {'data_scope': 'own',
                 'perms': ['module.quotes']}).status_code == 200
    assert _c('MREP').get('/quotes').status_code == 200


def test_preset_applies(people):
    r = _put(_c('MADMIN'), 'MHEAD', {'preset': 'vertical_head'})
    assert r.status_code == 200
    with flask_app.app_context():
        scope, perms = effective('MHEAD')
    assert scope == DataScope.VERTICAL
    assert 'reports.action' in perms


def test_reset_returns_to_role_default(people):
    c = _c('MADMIN')
    assert _put(c, 'MHEAD', {'preset': 'admin'}).status_code == 200
    with flask_app.app_context():
        assert effective('MHEAD')[0] == DataScope.ALL

    assert c.delete('/api/access/profile/MHEAD').status_code == 200
    with flask_app.app_context():
        assert AccessProfile.query.filter_by(emp_code='MHEAD').first() is None
        assert effective('MHEAD')[0] != DataScope.ALL


# ── guard rails ──────────────────────────────────────────────────────
def test_admin_cannot_remove_their_own_access(people):
    """Otherwise one wrong click locks the only door from the inside."""
    r = _put(_c('MADMIN'), 'MADMIN', {'data_scope': 'all', 'perms': []})
    assert r.status_code == 400
    assert _c('MADMIN').get('/api/access/matrix').status_code == 200


def test_unknown_permissions_are_dropped(people):
    assert _put(_c('MADMIN'), 'MREP',
                {'data_scope': 'own',
                 'perms': ['module.quotes', 'not.a.real.perm']}
                ).status_code == 200
    with flask_app.app_context():
        _s, perms = effective('MREP')
    assert 'not.a.real.perm' not in perms


def test_bad_scope_is_rejected(people):
    r = _put(_c('MADMIN'), 'MREP', {'data_scope': 'everything', 'perms': []})
    assert r.status_code == 400


def test_unknown_employee_is_404(people):
    r = _put(_c('MADMIN'), 'NOBODY9', {'data_scope': 'own', 'perms': []})
    assert r.status_code == 404


def test_api_access_me_needs_a_session(people):
    assert flask_app.test_client().get('/api/access/me').status_code == 401
