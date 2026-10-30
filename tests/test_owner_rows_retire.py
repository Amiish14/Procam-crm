"""
An owner row is retired, never deleted.

FINISH-TRACKER-CRM §3. The rollback for the TKT-0069 owner seed was
`DELETE FROM account_vertical_owner WHERE source='seeded'` — the one
irreversible step in an otherwise reversible release. Those rows are
the CRM's reading of who has actually served each account, derived
from lead history; deleting them threw away the reading and the fact
that it had been made, and re-running the seed later reads different
history.

`active=0` has the same effect on every reader and is one UPDATE
from being undone. The tests below hold that the effect really is
the same, that nothing is deleted, and that a person naming an owner
brings the row back rather than being shadowed by a withdrawn seed.
"""
import importlib
import importlib.util
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'retire-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'RetireTest-123456')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'retire.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
db = _main.db
Company, Employee = _main.Company, _main.Employee

PREFIX = 'RT '
REP, OTHER = 'RT_REP', 'RT_OTHER'
VERTICAL = 'Heavy Transport'


def _seed_script():
    path = os.path.join(_ROOT, 'scripts',
                        'seed_account_vertical_owners.py')
    spec = importlib.util.spec_from_file_location('seedowners', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _purge():
    from app.models.assignment import AccountVerticalOwner
    for account in Company.query.filter(
            Company.name.like(PREFIX + '%')).all():
        AccountVerticalOwner.query.filter_by(
            account_id=account.id).delete(synchronize_session=False)
    Company.query.filter(Company.name.like(PREFIX + '%')).delete(
        synchronize_session=False)
    Employee.query.filter(
        Employee.emp_code.in_([REP, OTHER])).delete(
            synchronize_session=False)
    db.session.commit()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        for code in (REP, OTHER):
            db.session.add(Employee(
                emp_code=code, name=code, password_hash='x', role='user',
                vertical=VERTICAL, is_active=True,
                email=code.lower() + '@procamlogistics.com'))
        db.session.commit()
        yield
        _purge()


def _account(name='Owned Ltd'):
    account = Company(name=PREFIX + name, is_active=True,
                      vertical=VERTICAL)
    db.session.add(account)
    db.session.flush()
    return account


def _row(account, code=REP, source='seeded', active=True):
    from app.models.assignment import AccountVerticalOwner
    row = AccountVerticalOwner(account_id=account.id, vertical=VERTICAL,
                               emp_code=code, source=source,
                               active=active)
    db.session.add(row)
    db.session.commit()
    return row


# ── the column ───────────────────────────────────────────────────────
def test_a_new_owner_row_is_live():
    with flask_app.app_context():
        row = _row(_account())
        assert row.active is True


def test_the_migration_is_additive_and_runs_twice():
    """--check twice, then the column, twice. A migration that
    refuses its second run is a migration nobody dares re-run."""
    import subprocess
    env = dict(os.environ)
    tmp = tempfile.mkdtemp()
    env['DATABASE_URL'] = 'sqlite:///' + os.path.join(tmp, 'mig.db')
    env['SECRET_KEY'] = 'migration'
    env['ADMIN_INITIAL_PASSWORD'] = 'MigrationTest-123456'
    script = os.path.join(_ROOT, 'scripts',
                          '2026_10_29_owner_rows_retire.py')
    for args in (['--check'], ['--check'], [], []):
        done = subprocess.run(
            [sys.executable, script] + args, env=env, cwd=_ROOT,
            capture_output=True, text=True, timeout=180)
        assert done.returncode == 0, done.stderr[-600:]
    assert 'already present' in done.stdout


# ── the readers ──────────────────────────────────────────────────────
def test_a_retired_row_is_not_an_owner():
    """The whole point: the effect must be identical to the delete
    it replaces."""
    with flask_app.app_context():
        from app.services import auto_assign
        account = _account()
        row = _row(account)
        assert auto_assign.owner_for_vertical(account.id,
                                              VERTICAL) == REP
        row.active = False
        db.session.commit()
        assert auto_assign.owner_for_vertical(account.id,
                                              VERTICAL) is None


def test_a_row_with_no_answer_yet_is_treated_as_live():
    """Rows written before the column existed read back NULL."""
    with flask_app.app_context():
        from app.services import auto_assign
        account = _account()
        row = _row(account)
        row.active = None
        db.session.commit()
        assert auto_assign.owner_for_vertical(account.id,
                                              VERTICAL) == REP


# ── the rollback ─────────────────────────────────────────────────────
def test_the_rollback_retires_and_deletes_nothing():
    with flask_app.app_context():
        from app.models.assignment import AccountVerticalOwner
        account = _account()
        _row(account)
        seed = _seed_script()
        seed._retire(retire=True, apply=True)
        row = AccountVerticalOwner.query.filter_by(
            account_id=account.id).one()
        assert row.active is False


def test_the_rollback_is_a_dry_run_without_apply():
    with flask_app.app_context():
        from app.models.assignment import AccountVerticalOwner
        account = _account()
        _row(account)
        _seed_script()._retire(retire=True, apply=False)
        assert AccountVerticalOwner.query.filter_by(
            account_id=account.id).one().active is True


def test_the_rollback_leaves_a_decision_alone():
    """Only `seeded` rows. A row somebody set by hand is a decision,
    and withdrawing a derivation must not withdraw that too."""
    with flask_app.app_context():
        from app.models.assignment import AccountVerticalOwner
        account = _account()
        _row(account, source='manual')
        _seed_script()._retire(retire=True, apply=True)
        assert AccountVerticalOwner.query.filter_by(
            account_id=account.id).one().active is True


def test_the_rollback_can_be_undone():
    with flask_app.app_context():
        from app.models.assignment import AccountVerticalOwner
        from app.services import auto_assign
        account = _account()
        _row(account)
        seed = _seed_script()
        seed._retire(retire=True, apply=True)
        seed._retire(retire=False, apply=True)
        assert AccountVerticalOwner.query.filter_by(
            account_id=account.id).one().active is True
        assert auto_assign.owner_for_vertical(account.id,
                                              VERTICAL) == REP


def test_the_rollback_writes_an_audit_row():
    with flask_app.app_context():
        from app.models.audit import AuditEvent
        account = _account()
        _row(account)
        _seed_script()._retire(retire=True, apply=True)
        event = AuditEvent.query.filter_by(
            action='account.owner_seed_retire').first()
        assert event is not None
        assert 'no row was deleted' in (event.reason or '')


def test_running_the_rollback_twice_changes_nothing_more():
    with flask_app.app_context():
        from app.models.assignment import AccountVerticalOwner
        account = _account()
        _row(account)
        seed = _seed_script()
        seed._retire(retire=True, apply=True)
        seed._retire(retire=True, apply=True)
        assert AccountVerticalOwner.query.filter_by(
            account_id=account.id).count() == 1


# ── a person outranks a withdrawn seed ───────────────────────────────
def test_naming_an_owner_brings_a_retired_row_back():
    with flask_app.app_context():
        from app.models.assignment import AccountVerticalOwner
        from app.services import auto_assign, lead_account
        account = _account()
        _row(account, code=REP)
        _seed_script()._retire(retire=True, apply=True)
        lead_account._teach(account, OTHER, actor='RT_ACTOR')
        db.session.commit()
        row = AccountVerticalOwner.query.filter_by(
            account_id=account.id).one()
        assert row.active is True
        assert row.emp_code == OTHER
        assert auto_assign.owner_for_vertical(account.id,
                                              VERTICAL) == OTHER
