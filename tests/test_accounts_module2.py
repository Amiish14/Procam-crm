"""
PROCAM-DECISIONS-02NOV Module 2 — accounts, ownership and merges.

The test that matters most here is
`test_two_gstins_in_one_cluster_are_never_merged`. Module 2.3 asks
for an auto-merge on a shared email domain, and the same pack says
only one Godrej company is blocked and the others are not. Godrej &
Boyce and Godrej Agrovet share godrej.com. `clusters()` joins on any
evidence, transitively, so an auto-merge on that rule would make two
legal entities one account — and then apply one block decision to
both.

Everything else here is the decision's own order of rules, held so
that a later edit cannot quietly change which rule wins.
"""
import importlib
import importlib.util
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'module2-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Module2Test12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'module2.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
flask_app.config['WTF_CSRF_ENABLED'] = False
Company, Employee, Lead = _main.Company, _main.Employee, _main.Lead

from app.models.assignment import AccountVerticalOwner   # noqa: E402
from app.services import accounts_module2 as m2          # noqa: E402

TAG = 'M2~'


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_ROOT, 'scripts', f'{name}.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PEOPLE = (
    ('DIR12010', 'Nilesh Kumar Sinha', True),
    ('DIR22010', 'James Francis Xavier', True),
    ('DIR72012', 'Srinivas Marella', True),
    ('EMP372011', 'Sanjna Vardhan', True),
    ('EMP472012', 'Gowdhaman Rajakrishnan', True),
    ('EMP572012', 'Vijay T V', True),
    ('EMP2882022', 'Seema Chattopadhyay', True),
    ('EMP2982023', 'Sharayu Uday Bhosale', True),
    ('EMP3962025', 'Dhanashree Harishchandra Pawar', True),
    ('EMP4092026', 'Pravin Choudhary', True),
    ('EMP3652025', 'Sumit Mondal', True),
    ('EMP172010', 'Manjurul Hoque', True),
    ('EMP242010', 'Laxmi Ram Singh', True),
    ('EMP1552018', 'Abhishek Singh', True),
    ('EMP3952025', 'Venkatesh Ramarao Althada', True),
)


def _emp(code, name, active=True):
    e = Employee.query.filter_by(emp_code=code).first() or \
        Employee(emp_code=code)
    e.name, e.is_active, e.must_change_pw = name, active, False
    e.role, e.session_version, e.is_super_admin = 'user', 0, False
    db.session.add(e)
    db.session.flush()
    return e


def _wipe():
    # The employees too, and this is not housekeeping. Every test
    # module in this suite sets DATABASE_URL with `setdefault`, so
    # the first one to run decides it and they all share one
    # database. This module seeds the real names the Module 2 rules
    # match on — 'James Francis Xavier' among them — and leaving
    # them behind made `_employee_by_name` in test_close_pending
    # find two people and refuse, which is exactly what it is
    # supposed to do. The test that failed was right; this fixture
    # was wrong.
    Employee.query.filter(
        Employee.emp_code.in_([c for c, _n, _a in PEOPLE])).delete(
            synchronize_session=False)
    ids = [a.id for a in Company.query.filter(
        Company.name.like(TAG + '%')).all()]
    if ids:
        AccountVerticalOwner.query.filter(
            AccountVerticalOwner.account_id.in_(ids)).delete(
                synchronize_session=False)
        Lead.query.filter(Lead.company_id.in_(ids)).delete(
            synchronize_session=False)
        Company.query.filter(Company.id.in_(ids)).delete(
            synchronize_session=False)
    Lead.query.filter(Lead.company.like(TAG + '%')).delete(
        synchronize_session=False)
    db.session.commit()


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        _wipe()
        for code, name, active in PEOPLE:
            _emp(code, name, active)
        db.session.commit()
        yield
        _wipe()


def _account(name, **kw):
    row = Company(name=TAG + name, is_active=True, **kw)
    db.session.add(row)
    db.session.flush()
    return row


def _owner(account, vertical, emp_code, source='seeded'):
    row = AccountVerticalOwner(account_id=account.id,
                               vertical=vertical, emp_code=emp_code,
                               source=source, active=True)
    db.session.add(row)
    db.session.flush()
    return row


def _lead(**kw):
    kw.setdefault('company', TAG + 'Enquiry')
    kw.setdefault('stage', 'New Opportunity')
    kw.setdefault('source', 'email')
    row = Lead(**kw)
    db.session.add(row)
    db.session.flush()
    return row


# ══ 2.3 the merge confidence — the Godrej case ══════════════════════
def test_two_gstins_in_one_cluster_are_never_merged(world):
    """A GSTIN is a legal identity. Godrej & Boyce and Godrej Agrovet
    share godrej.com and are different companies — the same pack
    blocks one and not the others."""
    cluster = {
        'accounts': [
            {'id': 1, 'name': 'Godrej and Boyce Mfg Co Ltd',
             'gstin': '27AAACG1234A1Z5', 'norm': 'godrejandboycemfgco'},
            {'id': 2, 'name': 'Godrej Agrovet Limited',
             'gstin': '27AAACG9876B1Z1', 'norm': 'godrejagrovet'},
        ],
        'reasons': ['shared email domain'],
        'losers': [{'id': 2}], 'survivor': {'id': 1},
    }
    verdict, why = m2.merge_confidence(cluster)
    assert verdict == m2.ASK
    assert 'legal identity' in why
    assert '2 different GSTINs' in why


def test_a_gstin_conflict_beats_every_other_match(world):
    """Even when the names normalise alike, two GSTINs are two
    companies."""
    cluster = {
        'accounts': [
            {'id': 1, 'name': 'Praj Industries Ltd',
             'gstin': '27AAACP1111A1Z5', 'norm': 'prajindustries'},
            {'id': 2, 'name': 'PRAJ INDUSTRIES LIMITED',
             'gstin': '24AAACP2222A1Z5', 'norm': 'prajindustries'},
        ],
        'reasons': ['names normalise alike', 'same GSTIN'],
        'losers': [{'id': 2}], 'survivor': {'id': 1},
    }
    assert m2.merge_confidence(cluster)[0] == m2.ASK


def test_a_shared_domain_alone_asks_a_person(world):
    """A corporate domain is shared by a group, not by a company."""
    cluster = {
        'accounts': [
            {'id': 1, 'name': 'Tata Steel Ltd', 'gstin': '',
             'norm': 'tatasteel'},
            {'id': 2, 'name': 'Tata Projects Ltd', 'gstin': '',
             'norm': 'tataprojects'},
        ],
        'reasons': ['shared email domain'],
        'losers': [{'id': 2}], 'survivor': {'id': 1},
    }
    verdict, why = m2.merge_confidence(cluster)
    assert verdict == m2.ASK
    assert 'group of companies' in why


def test_one_gstin_across_the_cluster_merges(world):
    cluster = {
        'accounts': [
            {'id': 1, 'name': 'Hacklin Logistics', 'gstin': '27AAA1Z5',
             'norm': 'hacklinlogistics'},
            {'id': 2, 'name': 'Hacklin Logistics Pvt Ltd',
             'gstin': '27AAA1Z5', 'norm': 'hacklinlogisticspvt'},
        ],
        'reasons': ['same GSTIN', 'shared email domain'],
        'losers': [{'id': 2}], 'survivor': {'id': 1},
    }
    verdict, why = m2.merge_confidence(cluster)
    assert verdict == m2.AUTO
    assert 'one GSTIN' in why


def test_names_that_normalise_alike_merge(world):
    """"Praj Industries Ltd" and "PRAJ INDUSTRIES LIMITED"."""
    cluster = {
        'accounts': [
            {'id': 1, 'name': 'Praj Industries Ltd', 'gstin': '',
             'norm': 'prajindustries'},
            {'id': 2, 'name': 'PRAJ INDUSTRIES LIMITED', 'gstin': '',
             'norm': 'prajindustries'},
        ],
        'reasons': ['names normalise alike'],
        'losers': [{'id': 2}], 'survivor': {'id': 1},
    }
    assert m2.merge_confidence(cluster)[0] == m2.AUTO


def test_names_that_merely_resemble_each_other_ask(world):
    """One GSTIN absent and two different normalised names is not
    evidence of one customer."""
    cluster = {
        'accounts': [
            {'id': 1, 'name': 'Andritz Hydro Pvt Ltd', 'gstin': '',
             'norm': 'andritzhydro'},
            {'id': 2, 'name': 'Andritz Technologies', 'gstin': '',
             'norm': 'andritztechnologies'},
        ],
        'reasons': ['shared email domain', 'names normalise alike'],
        'losers': [{'id': 2}], 'survivor': {'id': 1},
    }
    assert m2.merge_confidence(cluster)[0] == m2.ASK


def test_the_plan_splits_into_auto_and_ask(world):
    auto, ask = m2.merge_plan([
        {'accounts': [{'id': 1, 'name': 'A', 'gstin': 'G1',
                       'norm': 'a'},
                      {'id': 2, 'name': 'A Ltd', 'gstin': 'G1',
                       'norm': 'altd'}],
         'reasons': ['same GSTIN'], 'losers': [{'id': 2}],
         'survivor': {'id': 1}},
        {'accounts': [{'id': 3, 'name': 'B', 'gstin': 'G2',
                       'norm': 'b'},
                      {'id': 4, 'name': 'C', 'gstin': 'G3',
                       'norm': 'c'}],
         'reasons': ['shared email domain'], 'losers': [{'id': 4}],
         'survivor': {'id': 3}},
    ])
    assert len(auto) == 1 and len(ask) == 1
    assert auto[0]['verdict'] == m2.AUTO
    assert ask[0]['verdict'] == m2.ASK


# ══ 2.2 the owner rules, in the decision's order ════════════════════
def test_a_named_override_wins_over_everything(world):
    with flask_app.app_context():
        account = _account('BEML LIMITED')
        _owner(account, 'Heavy Transport', 'EMP372011')
        # And give the account history that rule 3 would otherwise
        # read differently.
        for _ in range(5):
            _lead(company=account.name, company_id=account.id,
                  procam_vertical='Heavy Transport',
                  assigned_to='EMP572012',
                  created_at=datetime.utcnow() - timedelta(days=5))
        db.session.commit()
        plan, problems, notes = m2.owner_plan()
        mine = [r for r in plan if r['account_id'] == account.id]
        assert mine, 'the override has to be applied'
        assert mine[0]['now'] == 'DIR22010'
        assert mine[0]['rule'].startswith('1 ')


def test_the_agreed_map_moves_crm_ownership(world):
    with flask_app.app_context():
        account = _account('Siemens Waluj Warehouse')
        _owner(account, 'Warehousing', 'EMP3962025')   # Dhanashree
        db.session.commit()
        plan, problems, notes = m2.owner_plan()
        mine = [r for r in plan if r['account_id'] == account.id]
        assert mine[0]['now'] == 'EMP4092026'          # Pravin
        assert mine[0]['rule'].startswith('2 ')


def test_the_pack_calls_active_people_inactive_and_it_is_reported(world):
    """Several names under "inactive owners" are is_active = 1 today.
    The list is applied as what it does — a re-pointing of CRM
    ownership — and the mismatch is said out loud rather than
    applied under a heading that does not match the data."""
    with flask_app.app_context():
        plan, problems, notes = m2.owner_plan()
        joined = ' '.join(notes)
        assert 'ACTIVE in the employee master' in joined
        assert 'Dhanashree' in joined


def test_nothing_in_module_2_deactivates_anybody(world):
    """"Do not deactivate her in Module 8" — and nothing here is
    Module 8."""
    import inspect

    # Code only: the module's comments discuss is_active at length,
    # and the first version of this test matched one of them rather
    # than an assignment. What must not exist is a *write*.
    code = '\n'.join(
        line.split('#', 1)[0]
        for line in inspect.getsource(m2).splitlines())
    assert '.is_active =' not in code
    assert 'is_active=False' not in code
    assert 'is_active=True' not in code
    with flask_app.app_context():
        account = _account('Dhanashree Account')
        _owner(account, 'Warehousing', 'EMP3962025')
        db.session.commit()
        m2.owner_plan()
        her = Employee.query.filter_by(emp_code='EMP3962025').first()
        assert her.is_active is True


def test_the_busiest_owner_wins_when_no_rule_names_one(world):
    with flask_app.app_context():
        account = _account('Ordinary Customer')
        _owner(account, 'Heavy Transport', 'DIR12010')
        for _ in range(4):
            _lead(company=account.name, company_id=account.id,
                  procam_vertical='Heavy Transport',
                  assigned_to='EMP572012',
                  created_at=datetime.utcnow() - timedelta(days=10))
        _lead(company=account.name, company_id=account.id,
              procam_vertical='Heavy Transport',
              assigned_to='DIR22010',
              created_at=datetime.utcnow() - timedelta(days=10))
        db.session.commit()
        plan, _p, _n = m2.owner_plan()
        mine = [r for r in plan if r['account_id'] == account.id]
        assert mine[0]['now'] == 'EMP572012'
        assert mine[0]['rule'].startswith('3 ')


def test_a_tie_goes_to_the_vertical_head_not_to_a_coin_toss(world):
    """"On a tie or no data: the vertical head." A tie broken by
    whichever employee code sorts first is not a decision."""
    with flask_app.app_context():
        from app.models.email_admin import VerticalHead

        row = VerticalHead.query.filter_by(
            vertical='Heavy Transport').first()
        if row is None:
            row = VerticalHead(vertical='Heavy Transport')
            db.session.add(row)
        row.head_emp_code, row.active = 'DIR22010', True
        account = _account('Tied Customer')
        _owner(account, 'Heavy Transport', 'DIR12010')
        for code in ('EMP572012', 'EMP372011'):
            for _ in range(3):
                _lead(company=account.name, company_id=account.id,
                      procam_vertical='Heavy Transport',
                      assigned_to=code,
                      created_at=datetime.utcnow() - timedelta(days=9))
        db.session.commit()
        from app.services import vertical_heads
        vertical_heads.cache_clear()
        plan, _p, _n = m2.owner_plan()
        mine = [r for r in plan if r['account_id'] == account.id]
        assert mine[0]['now'] == 'DIR22010'
        assert mine[0]['rule'].startswith('4 ')


def test_leads_older_than_four_months_do_not_count(world):
    with flask_app.app_context():
        from app.models.email_admin import VerticalHead

        row = VerticalHead.query.filter_by(
            vertical='Warehousing').first()
        if row is None:
            row = VerticalHead(vertical='Warehousing')
            db.session.add(row)
        row.head_emp_code, row.active = 'DIR72012', True
        account = _account('Dormant Customer')
        _owner(account, 'Warehousing', 'DIR12010')
        for _ in range(9):
            _lead(company=account.name, company_id=account.id,
                  procam_vertical='Warehousing',
                  assigned_to='EMP572012',
                  created_at=datetime.utcnow() - timedelta(days=200))
        db.session.commit()
        from app.services import vertical_heads
        vertical_heads.cache_clear()
        plan, _p, _n = m2.owner_plan()
        mine = [r for r in plan if r['account_id'] == account.id]
        assert mine[0]['rule'].startswith('4 ')


def test_a_name_that_matches_nobody_is_refused_not_guessed(world):
    """Tamal Das is not in the employee master. A wrong employee code
    in an owner column is silent: the account stops appearing on
    anybody's list."""
    with flask_app.app_context():
        account = _account('Sarens India Pvt Ltd')
        _owner(account, 'Heavy Transport', 'DIR12010')
        db.session.commit()
        plan, problems, _n = m2.owner_plan()
        assert any('Tamal Das' in p for p in problems)
        # And nothing was written for that account under rule 1.
        mine = [r for r in plan if r['account_id'] == account.id]
        assert not any(r['rule'].startswith('1 ') for r in mine)


def test_an_owner_already_right_is_not_touched(world):
    with flask_app.app_context():
        account = _account('BEML Already Right')
        _owner(account, 'Heavy Transport', 'DIR22010')
        db.session.commit()
        plan, _p, _n = m2.owner_plan()
        assert not [r for r in plan if r['account_id'] == account.id]


# ══ 2.1 the queue ═══════════════════════════════════════════════════
def test_a_lead_whose_domain_is_known_is_linked(world):
    with flask_app.app_context():
        account = _account('Indo Tech Transformers',
                           email_domains=['indotech.example'])
        db.session.commit()
        lead = _lead(company='Indo Tech', email='rfq@indotech.example',
                     procam_vertical='Heavy Transport',
                     assigned_to='EMP572012')
        db.session.commit()
        rows, review = m2.autoclear_plan()
        mine = [r for r in rows if r['lead'].id == lead.id]
        assert mine, 'the rules have to place it'
        assert mine[0]['account_id'] == account.id
        assert mine[0]['rule'] == 'domain matches'


def test_a_free_mail_domain_is_not_a_company(world):
    """Matching on gmail.com would join every personal-address
    enquiry into one account."""
    with flask_app.app_context():
        _account('Gmail Trap', email_domains=['gmail.com'])
        db.session.commit()
        lead = _lead(company='Somebody New',
                     email='someone@gmail.com',
                     procam_vertical='Heavy Transport',
                     assigned_to='EMP572012')
        db.session.commit()
        rows, _review = m2.autoclear_plan()
        mine = [r for r in rows if r['lead'].id == lead.id]
        assert mine[0]['create'] is True
        assert mine[0]['account_name'] == 'Somebody New'


def test_no_match_creates_from_the_typed_name(world):
    with flask_app.app_context():
        lead = _lead(company='Brand New Customer Ltd',
                     email='buyer@brandnew.example',
                     procam_vertical='Warehousing',
                     assigned_to='DIR72012')
        db.session.commit()
        rows, _review = m2.autoclear_plan()
        mine = [r for r in rows if r['lead'].id == lead.id]
        assert mine[0]['create'] is True
        assert mine[0]['rule'] == 'create from the typed name'
        assert mine[0]['account_name'] == 'Brand New Customer Ltd'


def test_no_name_falls_back_to_the_domain(world):
    with flask_app.app_context():
        lead = _lead(company='', email='buyer@newcorp.example',
                     procam_vertical='Warehousing',
                     assigned_to='DIR72012')
        db.session.commit()
        rows, _review = m2.autoclear_plan()
        mine = [r for r in rows if r['lead'].id == lead.id]
        assert mine[0]['rule'] == 'create from the sender domain'
        assert mine[0]['account_name'] == 'Newcorp'


def test_neither_a_name_nor_a_domain_goes_to_the_review(world):
    with flask_app.app_context():
        lead = _lead(company='', email='',
                     procam_vertical='Warehousing')
        db.session.commit()
        _rows, review = m2.autoclear_plan()
        mine = [r for r in review if r['lead'].id == lead.id]
        assert mine, 'it has to be reported, not dropped'
        assert 'nothing to name an account after' in mine[0]['why']


def test_the_owner_is_the_lead_owner_first(world):
    with flask_app.app_context():
        lead = _lead(company='Owner Test Ltd',
                     email='x@ownertest.example',
                     procam_vertical='Heavy Transport',
                     assigned_to='EMP572012')
        db.session.commit()
        rows, _review = m2.autoclear_plan()
        mine = [r for r in rows if r['lead'].id == lead.id]
        assert mine[0]['owner'] == 'EMP572012'
        assert mine[0]['owner_why'] == 'the lead owner'


def test_an_unowned_lead_goes_to_the_vertical_head(world):
    with flask_app.app_context():
        from app.models.email_admin import VerticalHead
        from app.services import vertical_heads

        row = VerticalHead.query.filter_by(
            vertical='Warehousing').first()
        if row is None:
            row = VerticalHead(vertical='Warehousing')
            db.session.add(row)
        row.head_emp_code, row.active = 'DIR72012', True
        db.session.commit()
        vertical_heads.cache_clear()
        lead = _lead(company='Headless Ltd',
                     email='x@headless.example',
                     procam_vertical='Warehousing', assigned_to=None)
        db.session.commit()
        rows, _review = m2.autoclear_plan()
        mine = [r for r in rows if r['lead'].id == lead.id]
        assert mine[0]['owner'] == 'DIR72012'
        assert 'head' in mine[0]['owner_why']


def test_an_ambiguous_name_waits_for_the_merge(world):
    """Two accounts matching one name is the ambiguous *master* that
    2.3 exists to fix, not a question about this lead."""
    with flask_app.app_context():
        _account('Meiden India')
        _account('MEIDEN INDIA')
        db.session.commit()
        lead = _lead(company=TAG + 'Meiden India',
                     email='x@meiden.example',
                     procam_vertical='Heavy Transport',
                     assigned_to='EMP572012')
        db.session.commit()
        rows, review = m2.autoclear_plan()
        in_review = [r for r in review if r['lead'].id == lead.id]
        in_rows = [r for r in rows if r['lead'].id == lead.id]
        assert in_review and not in_rows
        assert 'merge them first' in in_review[0]['why']


# ══ the script: preview, then save ══════════════════════════════════
def test_the_script_writes_nothing_on_check(world):
    """Rule 0.3: a preview with counts per rule, saved after an OK."""
    script = _load('2026_11_02_accounts_module2')
    with flask_app.app_context():
        account = _account('Check Only Ltd')
        _owner(account, 'Warehousing', 'EMP3962025')
        db.session.commit()
        before = AccountVerticalOwner.query.filter_by(
            account_id=account.id).first().emp_code

        plan, problems, notes = m2.owner_plan()
        script.report_owners(plan, problems, notes)
        db.session.rollback()
        after = AccountVerticalOwner.query.filter_by(
            account_id=account.id).first().emp_code
        assert before == after == 'EMP3962025'


def test_applying_the_owners_is_reversible(world):
    script = _load('2026_11_02_accounts_module2')
    with flask_app.app_context():
        from app.services import bulk_log

        account = _account('Reversible Ltd')
        _owner(account, 'Warehousing', 'EMP3962025')
        db.session.commit()

        plan = [r for r in m2.owner_plan()[0]
                if r['account_id'] == account.id]
        with bulk_log.batch('test.module2', actor='TEST') as log:
            script.apply_owners(plan, log)
            name = log.name
        db.session.commit()

        row = AccountVerticalOwner.query.filter_by(
            account_id=account.id).first()
        assert row.emp_code == 'EMP4092026'
        assert '2 Nov 2026' in (row.note or '')

        bulk_log.undo(name)
        db.session.commit()
        row = AccountVerticalOwner.query.filter_by(
            account_id=account.id).first()
        assert row.emp_code == 'EMP3962025'


def test_a_created_account_carries_a_dated_remark(world):
    """Rule 0.2: every changed record gets a dated audit remark."""
    script = _load('2026_11_02_accounts_module2')
    with flask_app.app_context():
        from app.services import bulk_log

        lead = _lead(company='Remark Test Ltd',
                     email='x@remarktest.example',
                     procam_vertical='Warehousing',
                     assigned_to='DIR72012')
        db.session.commit()
        rows = [r for r in m2.autoclear_plan()[0]
                if r['lead'].id == lead.id]
        with bulk_log.batch('test.module2.queue', actor='TEST') as log:
            linked, created = script.apply_queue(rows, log)
        db.session.commit()
        assert (linked, created) == (1, 1)
        account = Company.query.filter_by(
            name='Remark Test Ltd').first()
        assert account is not None
        assert '2 Nov 2026' in (account.notes or '')
        assert db.session.get(Lead, lead.id).company_id == account.id
