"""What the guessable-password reset is allowed to do, and what it must
never do.

The dangerous part of this tool is not the reset — it is the file of live
credentials it produces. So most of what is held here is about restraint:
the preview writes nothing, --apply refuses to run unasked, the super
admin is left alone, an account with a real password is not touched, and
the new password reaches the file and nowhere else — not stdout, not the
audit trail.
"""
import csv
import importlib
import os
import stat
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'password-reset-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'PwResetTest12345')
# setdefault, not assignment: the suite shares one database, and whichever
# module imports the app first decides where it lives.
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'pwreset.db'))

_main = importlib.import_module('app')                     # noqa: E402
importlib.import_module('app.models')                      # noqa: E402
flask_app, db, Employee = _main.app, _main.db, _main.Employee

sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
import reset_guessable_passwords as rgp                    # noqa: E402
from app.models.audit import AuditEvent                    # noqa: E402

WEAK_A, WEAK_B = 'PWRA', 'PWRB'
STRONG = 'PWRSTRONG'
SUPER = 'PWRSUPER'
#: What every account starts the fixture on, so "the session ended" is a
#: change from a known number rather than from zero.
START_VERSION = 3

STRONG_PASSWORD = 'tG7-marmalade-quay'


def _accounts():
    """(code, role, is_super, password). The weak ones are on their own
    employee code in lowercase, which is the state this tool exists for."""
    return (
        (WEAK_A, 'user', False, WEAK_A.lower()),
        (WEAK_B, 'admin', False, WEAK_B.lower()),
        (STRONG, 'user', False, STRONG_PASSWORD),
        (SUPER, 'admin', True, SUPER.lower()),
    )


@pytest.fixture(autouse=True)
def _preview_reads_the_live_database(monkeypatch):
    """The preview opens the database by DATABASE_URL, read-only.

    The suite shares one application, bound to whichever module imported
    it first, so the environment variable this module set at import may
    name a file that was never created. Point it at the database the
    app actually opened.
    """
    monkeypatch.setenv('DATABASE_URL',
                       flask_app.config['SQLALCHEMY_DATABASE_URI'])


@pytest.fixture()
def world(tmp_path):
    with flask_app.app_context():
        db.create_all()
        # Only ever clear this module's own rows: the suite shares one
        # database, and deleting everything wipes other modules' worlds.
        AuditEvent.query.filter(AuditEvent.entity_id.like('PWR%')).delete(
            synchronize_session=False)
        Employee.query.filter(Employee.emp_code.like('PWR%')).delete(
            synchronize_session=False)
        db.session.commit()
        for code, role, is_super, password in _accounts():
            emp = Employee(emp_code=code, name=f'Account {code}',
                           email=f'{code.lower()}@example.test', role=role,
                           is_active=True, is_super_admin=is_super,
                           must_change_pw=False,
                           session_version=START_VERSION)
            emp.set_password(password)
            db.session.add(emp)
        db.session.commit()
        # A path inside the test's own directory, so a run that writes
        # nothing is visible as a file that never appeared.
        yield str(tmp_path / 'out' / 'reset.csv')
        AuditEvent.query.filter(AuditEvent.entity_id.like('PWR%')).delete(
            synchronize_session=False)
        Employee.query.filter(Employee.emp_code.like('PWR%')).delete(
            synchronize_session=False)
        db.session.commit()


def _emp(code):
    return Employee.query.filter_by(emp_code=code).first()


def _state(code):
    e = _emp(code)
    return {'hash': e.password_hash, 'must_change_pw': bool(e.must_change_pw),
            'session_version': e.session_version or 0,
            'expires_at': e.temp_password_expires_at}


def _rows(path):
    with open(path, newline='', encoding='utf-8') as fh:
        return list(csv.DictReader(fh))


def _audit_text(code):
    """Everything the trail holds about this account, as one string."""
    events = AuditEvent.query.filter_by(entity_id=code).all()
    return ' '.join(f'{e.action} {e.actor} {e.reason} {e.old_value} '
                    f'{e.new_value}' for e in events)


# ── the preview ─────────────────────────────────────────────────────────

def test_preview_lists_the_exposed_accounts_and_writes_nothing(world, capsys):
    with flask_app.app_context():
        before = {c: _state(c) for c, _, _, _ in _accounts()}
        assert rgp.main(['--only', f'{WEAK_A},{WEAK_B},{STRONG}']) == 0
        out = capsys.readouterr().out
        assert 'PREVIEW' in out
        assert WEAK_A in out and WEAK_B in out
        # The one with a real password is not on the list.
        assert STRONG not in out
        # Role and administrator status are shown, so whoever reads the
        # preview can see which of these matter most.
        assert 'ADMIN' in out and 'admin' in out
        db.session.expire_all()
        assert {c: _state(c) for c, _, _, _ in _accounts()} == before
        assert not os.path.exists(world)
        assert _audit_text(WEAK_A).count('password_change') == 0


def test_preview_says_why_the_super_admin_is_left_alone(world, capsys):
    with flask_app.app_context():
        assert rgp.main([]) == 0
        out = capsys.readouterr().out
        assert f'skipped  {SUPER}' in out
        assert 'super admin' in out


def test_select_skips_the_super_admin_until_it_is_named(world):
    with flask_app.app_context():
        rows = [(e.emp_code, e.name, e.email, e.role, bool(e.is_super_admin),
                 e.password_hash)
                for e in Employee.query.filter(
                    Employee.emp_code.like('PWR%')).all()]
        chosen, skipped = rgp.select(rows)
        assert SUPER not in [a['emp_code'] for a in chosen]
        assert SUPER in [a['emp_code'] for a, _ in skipped]
        chosen, skipped = rgp.select(rows, only={SUPER})
        assert [a['emp_code'] for a in chosen] == [SUPER]
        assert skipped == []


# ── the refusals ────────────────────────────────────────────────────────

def test_apply_without_yes_refuses_and_changes_nothing(world, capsys):
    with flask_app.app_context():
        before = _state(WEAK_A)
        assert rgp.main(['--apply', '--only', WEAK_A, '--out', world]) == 2
        out = capsys.readouterr().out
        assert '--yes' in out and 'refused' in out
        db.session.expire_all()
        assert _state(WEAK_A) == before
        assert _emp(WEAK_A).check_password(WEAK_A.lower())
        assert not os.path.exists(world)


# ── the reset ───────────────────────────────────────────────────────────

def test_apply_replaces_the_password_and_ends_the_session(world, capsys):
    with flask_app.app_context():
        strong_before = _state(STRONG)
        assert rgp.main(['--apply', '--yes', '--actor', 'PWROPS',
                         '--only', f'{WEAK_A},{WEAK_B},{STRONG}',
                         '--out', world]) == 0
        out = capsys.readouterr().out
        rows = {r['emp_code']: r for r in _rows(world)}

        # Only the guessable ones; a real password is left alone entirely.
        assert set(rows) == {WEAK_A, WEAK_B}
        db.session.expire_all()
        assert _state(STRONG) == strong_before
        assert _emp(STRONG).check_password(STRONG_PASSWORD)

        for code in (WEAK_A, WEAK_B):
            emp = _emp(code)
            new_password = rows[code]['temporary_password']
            assert not emp.check_password(code.lower()), 'old one still works'
            assert emp.check_password(new_password)
            assert len(new_password) >= 12
            assert emp.must_change_pw is True
            assert (emp.session_version or 0) == START_VERSION + 1
            # 72 hours, the same rule an administrator-issued password has.
            expected = datetime.utcnow() + timedelta(hours=72)
            assert abs((emp.temp_password_expires_at
                        - expected).total_seconds()) < 300
            assert rows[code]['expires_at']
            assert rows[code]['name'] and rows[code]['email']

            # The password reached the file and nowhere else.
            assert new_password not in out
            assert new_password not in _audit_text(code)
            assert 'employee.password_change' in _audit_text(code)
            assert 'PWROPS' in _audit_text(code)

        assert os.path.abspath(world) in out
        assert '2 account(s)' in out
        mode = stat.S_IMODE(os.stat(world).st_mode)
        assert mode == 0o600, f'file is {oct(mode)}, not owner-only'


def test_only_and_exclude_narrow_the_run(world):
    with flask_app.app_context():
        before_b = _state(WEAK_B)
        assert rgp.main(['--apply', '--yes', '--only', f'{WEAK_A},{WEAK_B}',
                         '--exclude', WEAK_B, '--out', world]) == 0
        assert [r['emp_code'] for r in _rows(world)] == [WEAK_A]
        db.session.expire_all()
        assert _state(WEAK_B) == before_b
        assert _emp(WEAK_B).check_password(WEAK_B.lower())


def test_the_super_admin_is_reset_only_when_it_is_named(world, tmp_path):
    with flask_app.app_context():
        untouched = str(tmp_path / 'first.csv')
        assert rgp.main(['--apply', '--yes', '--only', WEAK_A,
                         '--out', untouched]) == 0
        db.session.expire_all()
        assert _emp(SUPER).check_password(SUPER.lower()), 'reset unasked'

        named = str(tmp_path / 'second.csv')
        assert rgp.main(['--apply', '--yes', '--only', SUPER,
                         '--out', named]) == 0
        db.session.expire_all()
        assert [r['emp_code'] for r in _rows(named)] == [SUPER]
        assert not _emp(SUPER).check_password(SUPER.lower())


def test_an_existing_file_is_never_overwritten(world):
    with flask_app.app_context():
        os.makedirs(os.path.dirname(world), exist_ok=True)
        with open(world, 'w', encoding='utf-8') as fh:
            fh.write('an earlier run\n')
        with pytest.raises(SystemExit):
            rgp.main(['--apply', '--yes', '--only', WEAK_A, '--out', world])
        db.session.expire_all()
        # The refusal rolled the reset back: the account still opens with
        # the old password rather than one nobody has a record of.
        assert _emp(WEAK_A).check_password(WEAK_A.lower())
        assert open(world, encoding='utf-8').read() == 'an earlier run\n'


def test_an_empty_database_is_refused(tmp_path, monkeypatch, capsys):
    import sqlite3
    path = str(tmp_path / 'empty.db')
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE employees (emp_code TEXT, name TEXT, '
                 'email TEXT, role TEXT, is_super_admin INTEGER, '
                 'is_active INTEGER, password_hash TEXT)')
    conn.commit()
    conn.close()
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///' + path)
    assert rgp.main([]) == 2
    assert 'refused' in capsys.readouterr().out
