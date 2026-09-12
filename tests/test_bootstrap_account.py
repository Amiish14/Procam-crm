"""PCM001 is not a person, and removing it must stick.

It is seeded from ADMIN_INITIAL_PASSWORD so a fresh install can be
opened at all. It holds role='admin', which in this codebase means every
permission, and its password lives in .env — a standing credential
nobody owns. Once real administrators exist it should be possible to
delete it and have it stay deleted.
"""
import importlib.util
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'BootTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'boot.db')

import importlib                                              # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db, Employee, Lead = (_main.app, _main.db, _main.Employee,
                                 _main.Lead)

_spec = importlib.util.spec_from_file_location(
    'lock_bootstrap',
    os.path.join(_ROOT, 'scripts', '2026_09_24_lock_bootstrap_account.py'))
lock = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lock)


@pytest.fixture(scope='module')
def world():
    with flask_app.app_context():
        db.create_all()
        for code, name in (('PCM001', 'Procam Super Admin'),
                           ('REALADM', 'A Real Admin')):
            e = Employee.query.filter_by(emp_code=code).first()
            if not e:
                e = Employee(emp_code=code, name=name)
                db.session.add(e)
            e.role, e.is_active, e.must_change_pw = 'admin', True, False
        db.session.commit()
    return True


def test_an_unreferenced_bootstrap_account_can_be_removed(world):
    with flask_app.app_context():
        assert lock.find_references(db, 'PCM001') == {}


def test_a_reference_is_found_wherever_it_hides(world):
    """The scan is derived from the live schema, so a table added later
    is covered without anyone remembering to add it here."""
    with flask_app.app_context():
        lead = Lead(company='Bootstrap Owned Ltd', source='manual',
                    assigned_to='PCM001')
        db.session.add(lead)
        db.session.commit()

        refs = lock.find_references(db, 'PCM001')
        assert refs.get('leads.assigned_to') == 1

        db.session.delete(lead)
        db.session.commit()
        assert lock.find_references(db, 'PCM001') == {}


def test_columns_that_only_look_like_emp_codes_are_skipped(world):
    """leads.secondary_owner_name holds a person's name, not a code;
    counting it would block deletion for no reason."""
    assert ('leads', 'secondary_owner_name') in lock._NOT_EMP_CODES
    assert ('lead_attachments', 'size_bytes') in lock._NOT_EMP_CODES


def test_the_bootstrap_account_is_not_recreated_when_admins_exist():
    """Without this, deleting PCM001 is undone by the next restart."""
    src = open(os.path.join(_ROOT, 'app.py')).read()
    block = src.split("pcm = Employee.query.filter_by(emp_code='PCM001')")[1]
    block = block[:1400]
    assert '_real_admin' in block, \
        'the seed must check for a real admin before recreating PCM001'
    assert 'not re-seeding' in block


def test_a_genuinely_empty_install_still_gets_a_way_in():
    """The bootstrap must still fire when nothing else can sign in —
    otherwise a fresh deployment is unopenable."""
    src = open(os.path.join(_ROOT, 'app.py')).read()
    block = src.split("pcm = Employee.query.filter_by(emp_code='PCM001')")[1]
    block = block[:1800]
    assert 'ADMIN_INITIAL_PASSWORD' in block
    assert 'elif not pcm:' in block, \
        'the seeding branch must remain reachable'
