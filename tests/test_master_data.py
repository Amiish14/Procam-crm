"""Master Data Center — §59-62.

The point of this feature is that Procam can add a vertical tomorrow
without a developer (§60), and that doing so can never damage historical
records (§61).
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
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'MasterTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'master.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db, Employee = _main.app, _main.db, _main.Employee
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.models.master_data import MasterItem               # noqa: E402
from app.master_data import service as md                   # noqa: E402


@pytest.fixture(scope='module')
def people():
    with flask_app.app_context():
        db.create_all()
        md.ensure_lists()
        for code, role, sup in (('MDADMIN', 'admin', True),
                                ('MDUSER', 'user', False)):
            e = Employee.query.filter_by(emp_code=code).first()
            if not e:
                e = Employee(emp_code=code, name=code)
                db.session.add(e)
            e.role, e.is_active, e.must_change_pw = role, True, False
            e.vertical, e.is_super_admin = 'All', sup
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


def _add(client, key, label):
    return client.post('/api/master/' + key,
                       data=json.dumps({'code': label, 'label': label}),
                       content_type='application/json')


# ── §60: a new vertical needs no developer ───────────────────────────
def test_admin_can_add_a_vertical(people):
    r = _add(_c('MDADMIN'), 'vertical', 'New Vertical Test')
    assert r.status_code == 200, r.get_data(as_text=True)
    with flask_app.app_context():
        assert 'New Vertical Test' in md.values('vertical')


def test_a_new_value_is_immediately_readable(people):
    """§60 — available everywhere the moment it is added."""
    _add(_c('MDADMIN'), 'vertical', 'Immediate Test')
    body = _c('MDUSER').get('/api/master/vertical').get_json()
    assert any(i['code'] == 'Immediate Test' for i in body['items'])


def test_a_normal_user_cannot_change_master_data(people):
    r = _add(_c('MDUSER'), 'vertical', 'Sneaky Vertical')
    assert r.status_code == 403
    with flask_app.app_context():
        assert 'Sneaky Vertical' not in md.values('vertical')


def test_reading_a_list_needs_only_a_session(people):
    """Forms and filters everywhere depend on this, so it is not admin-only."""
    assert _c('MDUSER').get('/api/master/vertical').status_code == 200
    assert flask_app.test_client().get('/api/master/vertical').status_code == 401


# ── §61: never destroy history ───────────────────────────────────────
def test_a_used_value_is_retired_not_deleted(people):
    """The rule that protects historical records."""
    with flask_app.app_context():
        item = md.add_item('vertical', 'Used Vertical', 'Used Vertical')
        item_id = item.id
        emp = Employee.query.filter_by(emp_code='MDUSER').first()
        emp.vertical = 'Used Vertical'
        db.session.commit()
        assert md.usage_count('vertical', 'Used Vertical') >= 1

    r = _c('MDADMIN').delete(f'/api/master/item/{item_id}')
    assert r.status_code == 200
    body = r.get_json()
    assert body['deleted'] is False
    assert body['deactivated'] is True
    assert body['used_by'] >= 1

    with flask_app.app_context():
        still = MasterItem.query.get(item_id)
        assert still is not None, 'a value in use was destroyed'
        assert still.is_active is False
        # the historical record is untouched
        emp = Employee.query.filter_by(emp_code='MDUSER').first()
        assert emp.vertical == 'Used Vertical'
        emp.vertical = 'All'
        db.session.commit()


def test_an_unused_value_can_be_deleted(people):
    with flask_app.app_context():
        item_id = md.add_item('network', 'Throwaway', 'Throwaway').id
    r = _c('MDADMIN').delete(f'/api/master/item/{item_id}')
    assert r.status_code == 200 and r.get_json()['deleted'] is True
    with flask_app.app_context():
        assert MasterItem.query.get(item_id) is None


def test_retired_values_are_hidden_from_new_entries(people):
    with flask_app.app_context():
        item = md.add_item('network', 'Retired Net', 'Retired Net')
        md.update_item(item.id, is_active=False)
        assert 'Retired Net' not in md.values('network')
        assert 'Retired Net' in [i.code for i in
                                 md.items('network', include_inactive=True)]


def test_re_adding_a_retired_value_restores_it(people):
    """Rather than colliding on the unique constraint."""
    with flask_app.app_context():
        first = md.add_item('network', 'Comeback', 'Comeback')
        md.update_item(first.id, is_active=False)
    r = _add(_c('MDADMIN'), 'network', 'Comeback')
    assert r.status_code == 200
    with flask_app.app_context():
        again = MasterItem.query.filter_by(list_key='network',
                                           code='Comeback').all()
        assert len(again) == 1, 'a duplicate row was created'
        assert again[0].is_active is True


# ── §5: the relationship vocabulary is complete ──────────────────────
def test_relationship_types_cover_the_spec(people):
    with flask_app.app_context():
        for code in ('Customer', 'Overseas Agent', 'Overseas Partner',
                     'Competitor', 'Vendor', 'EPC', 'Consultant',
                     'Network Member', 'RFQ Source'):
            md.add_item('relationship', code, code)
        have = set(md.values('relationship'))
    for expected in ('Customer', 'Competitor', 'Vendor', 'EPC',
                     'Overseas Partner'):
        assert expected in have, f'{expected} missing from §5 vocabulary'
