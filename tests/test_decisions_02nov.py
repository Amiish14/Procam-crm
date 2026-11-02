"""
PROCAM-DECISIONS-02NOV — the decisions the pack confirms, and the
earlier ones it withdraws.

The tests that matter here are the ones about a decision that has
been made twice in opposite directions. Code that reads correctly on
its own can still be wrong because a later decision replaced it, and
the only guard against that is an assertion that names the date.
"""
import importlib.util
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'decisions-02nov-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Decisions02Nov123')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'dec02nov.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Employee = _main.Employee


def _load(name):
    """A script, by path — scripts/ holds an `email_ingest.py` that
    shadows the package of the same name if the directory goes on
    sys.path."""
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_ROOT, 'scripts', f'{name}.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FOUR = ('DIR12010', 'DIR42010', 'EMP3892025', 'EMP372011')


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        for code, name in (('DIR12010', 'Nilesh Kumar Sinha'),
                           ('DIR42010', 'Tg Ramalingam'),
                           ('EMP3892025', 'Bhavin Vinodhbhai Jiilka'),
                           ('EMP372011', 'Sanjna Vardhan'),
                           ('DIR52011', 'S Sethupathy'),
                           ('EMP472012', 'Gowdhaman Rajakrishnan'),
                           ('EMP3962025', 'Dhanashree Harishchandra Pawar')):
            emp = Employee.query.filter_by(emp_code=code).first() or \
                Employee(emp_code=code)
            emp.name, emp.is_active = name, True
            emp.must_change_pw, emp.role = False, 'user'
            emp.session_version = 0
            db.session.add(emp)
        db.session.commit()
        yield


# ── the Installation head, decided twice ────────────────────────────
def test_the_installation_head_is_sethupathy_everywhere(world):
    """24 Oct said Gowdhaman; 2 Nov says Sethupathy and withdraws it."""
    import scripts.vertical_master_apply as vm

    nov = _load('2026_11_02_installation_head')
    assert vm.HEADS['Installation'] == 'DIR52011'
    assert nov.NEW_HEAD == 'DIR52011'


def test_the_october_head_script_cannot_run(world):
    oct25 = _load('2026_10_25_installation_head')
    with pytest.raises(SystemExit) as caught:
        oct25.main()
    assert 'superseded' in str(caught.value).lower()


def test_the_october_head_script_still_records_what_it_wrote(world):
    """The row it wrote is what the November script has to find, so
    the value belongs somewhere a reader can see it."""
    oct25 = _load('2026_10_25_installation_head')
    nov = _load('2026_11_02_installation_head')
    assert oct25.NEW_HEAD == nov.OCTOBER_HEAD == 'EMP472012'


def test_gowdhaman_is_still_an_account_owner(world):
    """Heading a vertical and serving customers in it are different
    jobs. Module 2 gives him Nippon Express; the head row moving must
    not touch that."""
    closing = _load('2026_11_01_close_pending')
    assert ('Nippon Express', 'Gowdhaman Rajakrishnan') in \
        tuple(closing.ACCOUNT_OWNERS)


# ── FF buy rates and margin: exactly four ───────────────────────────
def test_the_ff_margin_viewers_are_the_four_named(world):
    from app.services import admin_settings

    with flask_app.app_context():
        assert sorted(admin_settings.get('margin_viewers_ff')) == \
            sorted(FOUR)


def test_the_ff_viewer_script_names_the_same_four(world):
    mod = _load('2026_11_02_ff_margin_viewers')
    assert sorted(c for c, _ in mod.WANTED) == sorted(FOUR)


def test_every_ff_viewer_is_an_employee_on_file(world):
    """A code that is not an employee grants the right to nobody and
    hides the numbers from everybody."""
    mod = _load('2026_11_02_ff_margin_viewers')
    with flask_app.app_context():
        for code, who in mod.WANTED:
            emp = Employee.query.filter_by(emp_code=code).first()
            assert emp is not None, f'{who} -> {code}'


def test_a_margin_viewer_is_not_a_role(world, monkeypatch):
    """"Every admin" is a larger and growing group than the four the
    decision names."""
    from app.models.quote import Quote
    from app.services import admin_settings, margin_policy

    with flask_app.app_context():
        admin_settings.save('margin_viewers_ff', list(FOUR),
                            actor='test')
        emp = Employee.query.filter_by(emp_code='DIR52011').first()
        emp.role = 'admin'
        emp.is_super_admin = True
        db.session.commit()
        monkeypatch.setattr(margin_policy, 'quote_vertical',
                            lambda q: 'Project Freight')
        quote = Quote(quote_number='DEC02NOV-1',
                      prepared_by_id='DIR12010')
        # An admin, unrestricted in scope, and still not one of the
        # four — so the buy rate stays hidden. The scope check below
        # the vertical branch would have let him through.
        assert margin_policy.can_see_buy(quote,
                                         emp_code='DIR52011') is False
        assert margin_policy.can_see_buy(quote,
                                         emp_code='EMP3892025') is True


# ── Dhanashree stays active ─────────────────────────────────────────
def test_dhanashree_is_not_on_any_deactivation_list(world):
    """The pack says so twice, once under the confirmed decisions and
    again in Module 8. Module 2 moves her CRM lead and account
    ownership to Pravin; that is all."""
    closing = _load('2026_11_01_close_pending')
    names = [n for _, n in closing.ACCOUNT_OWNERS]
    assert 'Dhanashree Harishchandra Pawar' not in names
