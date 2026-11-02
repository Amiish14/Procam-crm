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
        rows, review, _stays, _nr = m2.autoclear_plan()
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
        rows, _review, _stays, _nr = m2.autoclear_plan()
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
        rows, _review, _stays, _nr = m2.autoclear_plan()
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
        rows, _review, _stays, _nr = m2.autoclear_plan()
        mine = [r for r in rows if r['lead'].id == lead.id]
        assert mine[0]['rule'] == 'create from the sender domain'
        assert mine[0]['account_name'] == 'Newcorp'


def test_neither_a_name_nor_a_domain_stays_in_account_check(world):
    """It went to Nilesh's Thursday review until 2 Nov; now it stays.

    Nothing about it needs a decision — it needs somebody to read the
    enquiry and type the customer's name, which is Account check's job
    and not a thing to put on a director's Thursday.
    """
    with flask_app.app_context():
        lead = _lead(company='', email='',
                     procam_vertical='Warehousing')
        db.session.commit()
        _rows, review, stays, _nr = m2.autoclear_plan()
        mine = [r for r in stays if r['lead'].id == lead.id]
        assert mine, 'it has to be reported, not dropped'
        assert 'no company name' in mine[0]['why']
        assert not [r for r in review if r['lead'].id == lead.id]


def test_the_owner_is_the_lead_owner_first(world):
    with flask_app.app_context():
        lead = _lead(company='Owner Test Ltd',
                     email='x@ownertest.example',
                     procam_vertical='Heavy Transport',
                     assigned_to='EMP572012')
        db.session.commit()
        rows, _review, _stays, _nr = m2.autoclear_plan()
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
        rows, _review, _stays, _nr = m2.autoclear_plan()
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
        rows, review, _stays, _nr = m2.autoclear_plan()
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


def test_nothing_is_created_in_bulk(world):
    """Decided 2 Nov. The leads stay in Account check.

    The proposal is still made and still reported — the CSV is the list
    the vertical heads work from — and `apply_queue` writes none of it.
    Linking is untouched: an exact domain or name match is not a new
    record.
    """
    script = _load('2026_11_02_accounts_module2')
    assert m2.CREATE_ACCOUNTS is False
    with flask_app.app_context():
        from app.services import bulk_log

        lead = _lead(company='Nocreate Test Ltd',
                     email='x@nocreatetest.example',
                     procam_vertical='Warehousing',
                     assigned_to='DIR72012')
        db.session.commit()
        rows = [r for r in m2.autoclear_plan()[0]
                if r['lead'].id == lead.id]
        assert rows and rows[0]['create'] is True, \
            'it is still proposed, so the heads have a list'
        with bulk_log.batch('test.module2.nocreate', actor='TEST') as log:
            linked, created, held = script.apply_queue(rows, log)
        db.session.commit()
        assert (created, held) == (0, 1)
        assert linked == 0, 'nothing to link it to either'
        assert Company.query.filter_by(
            name='Nocreate Test Ltd').first() is None
        assert db.session.get(Lead, lead.id).company_id is None


def test_a_created_account_carries_a_dated_remark(world, monkeypatch):
    """Rule 0.2: every changed record gets a dated audit remark.

    Creation is switched off, so the switch is flipped for this test
    alone. Kept rather than deleted: when a head confirms an account on
    screen the same remark has to appear, and this is the only thing
    that holds the creation path to it.
    """
    script = _load('2026_11_02_accounts_module2')
    monkeypatch.setattr(m2, 'CREATE_ACCOUNTS', True)
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
            linked, created, _held = script.apply_queue(rows, log)
        db.session.commit()
        assert (linked, created) == (1, 1)
        account = Company.query.filter_by(
            name='Remark Test Ltd').first()
        assert account is not None
        assert '2 Nov 2026' in (account.notes or '')
        assert db.session.get(Lead, lead.id).company_id == account.id


# ══ 2.1 what may never become an account ════════════════════════════
# The dry run proposed creating accounts called "Unknown" for leads
# #10096 and #10098, because rule 2 says "create the account from the
# name the user typed" and that is what somebody had typed. An account
# called Unknown is the fault `account_match.not_a_customer` already
# refuses to LINK to — #847 "New Lead" holds both fried-sped.de and
# jindalstainless.com — and the linking guard cannot catch it, because
# at that moment the account does not exist yet.
def test_a_placeholder_typed_name_creates_nothing(world):
    with flask_app.app_context():
        lead = _lead(company='Unknown',
                     email='buyer@realcustomer.example',
                     procam_vertical='Heavy Transport',
                     assigned_to='EMP572012')
        db.session.commit()
        rows, _review, stays, _nr = m2.autoclear_plan()
        assert not [r for r in rows if r['lead'].id == lead.id], \
            'no rule may clear a lead whose account would be "Unknown"'
        mine = [s for s in stays if s['lead'].id == lead.id]
        assert mine, 'it has to stay in Account check'
        assert 'placeholder' in mine[0]['why']


@pytest.mark.parametrize('typed', ['Unknown', 'New Lead', 'To review',
                                   'Info', 'Email', '   ', '-', '...',
                                   'n/a', 'TBD'])
def test_the_whole_named_set_is_refused(world, typed):
    with flask_app.app_context():
        assert m2.why_not_an_account_name(typed), \
            f'{typed!r} must never name an account'


def test_our_own_name_is_refused_however_it_is_spelt(world):
    for typed in ('Procam', 'Procam Logistics', 'PROCAM GROUP',
                  'procam-logistics', 'Procam  Warehousing Pvt Ltd'):
        why = m2.why_not_an_account_name(typed)
        assert why, f'{typed!r} is us, not a customer'
        assert 'us, not a customer' in why


def test_our_own_mail_domain_is_refused(world):
    assert m2.why_not_an_account_name('Some Name',
                                      domain='procamlogistics.com')
    assert m2.why_not_an_account_name('Some Name',
                                      domain='mail.procamgroup.in')
    # and a customer's domain is not
    assert m2.why_not_an_account_name('Indo Tech',
                                      domain='indotech.example') == ''


def test_a_lead_forwarded_by_a_colleague_creates_nothing(world):
    """The sender is a colleague, so the domain is ours.

    `_name_from_domain('procamlogistics.com')` would produce an account
    called Procamlogistics and then swallow every later forward.
    """
    with flask_app.app_context():
        lead = _lead(company='', email='sanjna@procamlogistics.com',
                     procam_vertical='Project Freight',
                     assigned_to='EMP372012')
        db.session.commit()
        rows, _review, stays, _nr = m2.autoclear_plan()
        assert not [r for r in rows if r['lead'].id == lead.id]
        mine = [s for s in stays if s['lead'].id == lead.id]
        assert mine
        assert 'own mail domain' in mine[0]['why'] or \
            'us, not a customer' in mine[0]['why']


def test_a_real_name_is_still_created(world):
    """The guard must not be so wide that it stops the rule working."""
    with flask_app.app_context():
        lead = _lead(company='Walchandnagar Industries',
                     email='rfq@walchand.example',
                     procam_vertical='Heavy Transport',
                     assigned_to='EMP572012')
        db.session.commit()
        rows, _review, stays, _nr = m2.autoclear_plan()
        mine = [r for r in rows if r['lead'].id == lead.id]
        assert mine and mine[0]['create'] is True
        assert mine[0]['account_name'] == 'Walchandnagar Industries'
        assert not [s for s in stays if s['lead'].id == lead.id]


def test_the_shared_placeholder_list_is_the_only_one(world):
    """One list, read by the matcher, not_a_customer and this.

    A second copy is how a name gets added to one and not the other,
    and then an account nobody may link to is one this module happily
    creates.
    """
    from app.services import account_match
    source = open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'app', 'services', 'accounts_module2.py')).read()
    assert 'PLACEHOLDER_NAMES' in source
    for word in ('unknown', 'new lead', 'to review'):
        assert word in account_match.PLACEHOLDER_NAMES
        # and not written out again in this module's own code
        assert f"'{word}'" not in source


def test_the_csv_lists_every_proposed_account(world):
    """Thirty are printed; the thirty-first is the one that matters."""
    import csv as _csv
    import importlib.util
    import tempfile

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'scripts', '2026_11_02_accounts_module2.py')
    spec = importlib.util.spec_from_file_location('m2script', path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    with flask_app.app_context():
        leads = []
        for i in range(35):
            leads.append(_lead(company=f'Customer {i:02d}',
                               email=f'rfq@c{i:02d}.example',
                               procam_vertical='Heavy Transport',
                               assigned_to='EMP572012'))
        db.session.commit()
        rows, _review, _stays, _nr = m2.autoclear_plan()
        creates = [r for r in rows if r['create']]
        assert len(creates) >= 35

        out = os.path.join(tempfile.mkdtemp(), 'creates.csv')
        script.write_creates_csv(creates, out)
        with open(out, newline='', encoding='utf-8') as fh:
            read = list(_csv.reader(fh))
    assert read[0][:4] == ['lead', 'typed name', 'proposed account',
                           'owner']
    assert len(read) - 1 == len(creates), 'every row, not a sample'


# ══ 2.1 the three rules added on 2 Nov after the first dry run ══════
def _plain(name, **kw):
    """An account under its real name, with no test prefix.

    The squashed-name comparisons are the point of these tests, so the
    name has to be exactly what it would be in production.
    """
    row = Company(name=name, is_active=True, **kw)
    db.session.add(row)
    db.session.flush()
    return row


# ── names that are too short, or a word for what the business does ──
@pytest.mark.parametrize('typed,expect', [
    ('Se', 'too short'),
    ('ABB', 'too short'),
    ('Global', 'what the business does'),
    ('Group', 'what the business does'),
    ('Logistics', 'what the business does'),
    ('Transport', 'what the business does'),
])
def test_a_short_or_generic_name_creates_nothing(world, typed, expect):
    with flask_app.app_context():
        lead = _lead(company=typed, email=f'x@{typed.lower()}x.example',
                     procam_vertical='Heavy Transport',
                     assigned_to='EMP572012')
        db.session.commit()
        rows, _review, stays, _nr = m2.autoclear_plan()
        assert not [r for r in rows if r['lead'].id == lead.id]
        mine = [s for s in stays if s['lead'].id == lead.id]
        assert mine, f'{typed!r} has to reach Account check'
        assert expect in mine[0]['why']


def test_a_generic_word_inside_a_real_name_is_fine(world):
    """"Global" is refused; "Global Calcium" is a company."""
    with flask_app.app_context():
        assert m2.why_not_an_account_name('Global Calcium') == ''
        assert m2.why_not_an_account_name('Global')


def test_beml_is_exactly_long_enough(world):
    """The four-letter floor is the rule that costs the most."""
    assert m2.why_not_an_account_name('BEML') == ''
    assert m2.why_not_an_account_name('BEM')


# ── a name that is already an account under another spelling ────────
def test_a_missing_space_proposes_a_link_not_a_second_account(world):
    with flask_app.app_context():
        account = _plain('Hitachi Energy')
        db.session.commit()
        lead = _lead(company='Hitachienergy',
                     email='rfq@someother.example',
                     procam_vertical='Heavy Transport',
                     assigned_to='EMP572012')
        db.session.commit()
        rows, _review, stays, _nr = m2.autoclear_plan()
        assert not [r for r in rows if r['lead'].id == lead.id], \
            'it must not create a second Hitachi Energy'
        mine = [s for s in stays if s['lead'].id == lead.id]
        assert mine
        assert mine[0]['suggest'].id == account.id
        assert 'link it to the account that exists' in mine[0]['why']


def test_an_alias_is_what_resolves_lntecc(world):
    """"Lntecc" is L&T ECC, and only the alias can say so.

    A prefix of an account NAME is not enough — that would propose
    linking "Global Calcium" to "Global". An alias is a spelling
    somebody deliberately recorded, which a merge does automatically.
    """
    with flask_app.app_context():
        account = _plain('Larsen & Toubro', aliases=['LNT', 'L&T'])
        db.session.commit()
        lead = _lead(company='Lntecc', email='rfq@lntecc.example',
                     procam_vertical='Heavy Transport',
                     assigned_to='EMP572012')
        db.session.commit()
        rows, _review, stays, _nr = m2.autoclear_plan()
        assert not [r for r in rows if r['lead'].id == lead.id]
        mine = [s for s in stays if s['lead'].id == lead.id]
        assert mine
        assert mine[0]['suggest'].id == account.id
        assert 'LNT' in mine[0]['why']


def test_a_name_prefix_alone_does_not_propose_a_link(world):
    """Without the alias, nothing is proposed and nothing is wrong."""
    with flask_app.app_context():
        _plain('Global')
        db.session.commit()
        index = m2._alias_index(
            [c for c in Company.query.filter_by(is_active=True).all()])
        found, _how = m2.looks_like_existing('Global Calcium', '',
                                             index, {})
        assert found is None, \
            '"Global" is six letters; a prefix that short is a coincidence'


def test_the_senders_domain_resolves_the_account_with_nothing_typed(world):
    with flask_app.app_context():
        account = _plain('Westfracht')
        db.session.commit()
        lead = _lead(company='', email='ops@westfracht.example',
                     procam_vertical='Project Freight',
                     assigned_to='EMP372012')
        db.session.commit()
        rows, _review, stays, _nr = m2.autoclear_plan()
        assert not [r for r in rows if r['lead'].id == lead.id]
        mine = [s for s in stays if s['lead'].id == lead.id]
        assert mine and mine[0]['suggest'].id == account.id


# ── one account per company, however many ways it is spelt ──────────
def test_case_and_punctuation_variants_collapse_into_one(world):
    with flask_app.app_context():
        for i, typed in enumerate(['TQCERT', 'T.Q. Cert', 'tqcert',
                                   'TQ  CERT']):
            _lead(company=typed, email=f'a{i}@tq{i}.example',
                  procam_vertical='Heavy Transport',
                  assigned_to='EMP572012')
        db.session.commit()
        rows, _review, _stays, _nr = m2.autoclear_plan()
        creates = [r for r in rows if r['create']
                   and m2.squash(r['account_name']) == 'tqcert']
        assert len(creates) == 4, 'all four leads are placed'
        assert len({r['account_name'] for r in creates}) == 1, \
            'and they propose one account, not four'


def test_the_longest_spelling_wins_a_tie(world):
    """"Drewes Group" over "Drewes": the longer one carries more."""
    with flask_app.app_context():
        _lead(company='Drewes', email='a@d1.example',
              procam_vertical='Project Freight', assigned_to='EMP372012')
        _lead(company='Drewes Group', email='b@d2.example',
              procam_vertical='Project Freight', assigned_to='EMP372012')
        db.session.commit()
        rows, _review, _stays, _nr = m2.autoclear_plan()
        creates = [r for r in rows if r['create']
                   and m2.squash(r['account_name']) == 'drewes']
        assert len(creates) == 2
        assert creates[0]['account_name'] == 'Drewes Group'


def test_a_collapsed_row_says_what_was_typed_on_it(world):
    """A name no other test uses: the earlier Westfracht account would
    make this a LINK rather than a create, and the test would then be
    measuring the wrong rule."""
    with flask_app.app_context():
        _lead(company='Nordfracht', email='a@nf1.example',
              procam_vertical='Project Freight', assigned_to='EMP372012')
        odd = _lead(company='NORDFRACHT', email='b@nf2.example',
                    procam_vertical='Project Freight',
                    assigned_to='EMP372012')
        db.session.commit()
        rows, _review, _stays, _nr = m2.autoclear_plan()
        mine = [r for r in rows if r['lead'].id == odd.id]
        assert mine, 'it has to be a create, not a link'
        assert mine[0]['create'] is True
        assert mine[0]['account_name'] == 'Nordfracht'
        assert "spelt 'NORDFRACHT'" in mine[0]['why']


# ── leads that are not a customer at all ───────────────────────────
@pytest.mark.parametrize('subject,expect', [
    ('Our monthly newsletter — unsubscribe here', 'marketing'),
    ('Invitation: India Logistics Summit 2027 — early bird', 'events'),
    ('Premium listing package for your business', 'portals'),
    ('We offer marine cargo insurance cover at best rates', 'insurance'),
    ('We are a leading freight forwarder — agent network enquiry',
     'broker'),
])
def test_the_five_classes_are_recognised(world, subject, expect):
    with flask_app.app_context():
        lead = _lead(company='Somebody Ltd', email='x@sender.example',
                     original_email_subject=subject,
                     procam_vertical='Project Freight',
                     assigned_to='EMP372012')
        db.session.commit()
        code, why = m2.not_relevant_class(lead)
        assert code == expect, why


def test_a_not_relevant_lead_creates_no_account(world):
    with flask_app.app_context():
        lead = _lead(company='Conference Org Ltd',
                     email='events@someorganiser.example',
                     original_email_subject='Sponsorship for our summit',
                     procam_vertical='Project Freight',
                     assigned_to='EMP372012')
        db.session.commit()
        rows, _review, stays, nr = m2.autoclear_plan()
        assert not [r for r in rows if r['lead'].id == lead.id]
        assert not [s for s in stays if s['lead'].id == lead.id]
        mine = [n for n in nr if n['lead'].id == lead.id]
        assert mine and mine[0]['class'] == 'events'


def test_a_procurement_portal_is_not_a_not_relevant_class(world):
    """Two Ariba leads are open opportunities with owners right now.

    `junk_suggest` records the measurement that kept `noreply@*` off
    the block list for exactly this reason.
    """
    with flask_app.app_context():
        lead = _lead(company='Big Client Ltd',
                     email='no-reply@eusmtp.ariba.com',
                     original_email_subject=(
                         'RFQ invitation — register now on our portal'),
                     procam_vertical='Heavy Transport',
                     assigned_to='EMP572012')
        db.session.commit()
        code, _why = m2.not_relevant_class(lead)
        assert code is None, \
            'an Ariba RFQ invitation is how a large client sends an RFQ'


def test_a_real_enquiry_mentioning_insurance_is_not_a_solicitation(world):
    """Half of all freight enquiries say who insures the cargo."""
    with flask_app.app_context():
        lead = _lead(company='Indo Tech Transformers',
                     email='rfq@indotech.example',
                     original_email_subject=(
                         'Transformer movement Vadodara to Lapanga — '
                         'cargo insurance by us'),
                     procam_vertical='Heavy Transport',
                     assigned_to='EMP572012')
        db.session.commit()
        code, why = m2.not_relevant_class(lead)
        assert code is None, why


def test_the_block_list_is_read_not_copied(world):
    with flask_app.app_context():
        from app.models.master_data import MasterItem
        from app.services import system_senders
        db.session.add(MasterItem(
            list_key=system_senders.LIST_KEY,
            code='robot@someportal.example', label='robot',
            description='a robot', is_active=True))
        db.session.commit()
        lead = _lead(company='Somebody Ltd',
                     email='robot@someportal.example',
                     procam_vertical='Project Freight',
                     assigned_to='EMP372012')
        db.session.commit()
        code, why = m2.not_relevant_class(lead)
        assert code == 'blocked_sender'
        assert 'block list' in why
        # and the module holds no second copy of that list
        source = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'app', 'services', 'accounts_module2.py')).read()
        assert 'robot@someportal.example' not in source


# ── the sections run independently ─────────────────────────────────
def test_the_opt_in_section_is_not_in_the_default_set(world):
    import importlib.util
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'scripts', '2026_11_02_accounts_module2.py')
    spec = importlib.util.spec_from_file_location('m2script2', path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    assert 'notrelevant' not in script.SECTIONS, \
        'a bare --apply must not mark leads not relevant'
    assert 'notrelevant' in script.ALL_SECTIONS
    assert 'merges' in script.SECTIONS


# ══ the Not-relevant sender list ════════════════════════════════════
def test_a_listed_sender_is_not_relevant(world):
    """A second Master Data list, so future mail lands in the queue.

    Not the ingest block list: a blocked sender's mail never becomes a
    lead at all, so a real enquiry from a portal account would be
    invisible rather than filed in the right place.
    """
    with flask_app.app_context():
        from app.services import not_relevant_senders as nrs
        assert nrs.add('*@gastechevent.example', why='named 2 Nov',
                       actor='TEST') == 'added'
        db.session.commit()
        lead = _lead(company='Gastechevent',
                     email='news@gastechevent.example',
                     procam_vertical='Project Freight',
                     assigned_to='EMP372012')
        db.session.commit()
        code, why = m2.not_relevant_class(lead)
        assert code == 'listed_sender'
        assert 'gastechevent' in why


def test_the_two_lists_are_not_the_same_list(world):
    from app.services import not_relevant_senders as nrs
    from app.services import system_senders
    assert nrs.LIST_KEY != system_senders.LIST_KEY


def test_our_own_domain_can_never_be_listed(world):
    """Every internal forward would be filed as not relevant."""
    from app.services import not_relevant_senders as nrs
    for pattern in ('*@procamlogistics.com', '*@procamgroup.in',
                    '*@mail.procamgroup.in'):
        why = nrs.why_not_a_pattern(pattern)
        assert why, pattern
        assert 'our own' in why


def test_a_bare_domain_is_refused_because_it_would_never_fire(world):
    from app.services import not_relevant_senders as nrs
    why = nrs.why_not_a_pattern('gastechevent.com')
    assert 'no @ in it' in why
    assert '*@gastechevent.com' in why


def test_a_free_mailbox_provider_cannot_be_listed(world):
    from app.services import not_relevant_senders as nrs
    assert 'personal mailbox' in nrs.why_not_a_pattern('*@gmail.com')


def test_a_catch_all_is_refused(world):
    from app.services import not_relevant_senders as nrs
    for pattern in ('*@*', '*', '*@'):
        assert nrs.why_not_a_pattern(pattern)


def test_the_list_starts_empty_and_is_not_seeded_in_code(world):
    """A sender in code is a sender nobody can take off."""
    source = open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'app', 'services', 'not_relevant_senders.py')).read()
    assert 'SEEDS' not in source
    for name in ('gastechevent', 'luxuryescapes', 'marineinsight'):
        assert name not in source.lower()


# ══ the fifteen named senders ═══════════════════════════════════════
def test_the_dry_run_names_fifteen_and_writes_nothing(world):
    script = _load('2026_11_02_not_relevant_senders')
    assert len(script.NAMED) == 15
    with flask_app.app_context():
        lead = _lead(company='Bricsamail',
                     email='news@bricsa.example',
                     procam_vertical='Project Freight')
        db.session.commit()
        found = script.find(script.NAMED)
        assert lead.id in [l.id for l in found['Bricsamail']]
        rows = script.survey(found)
        mine = [r for r in rows if r['name'] == 'Bricsamail'][0]
        assert lead.id in [l.id for l in mine['eligible']]
        # the domain is READ off the lead, never guessed from the name
        assert 'bricsa.example' in mine['domains']
        rules = script.plan_rules(rows)
        assert '*@bricsa.example' in [p for p, _w in rules]
        # and nothing was written
        assert db.session.get(Lead, lead.id).junk_deleted is not True


def test_a_name_matching_no_lead_adds_no_rule(world):
    """A domain nobody has seen mail from is not a rule worth having."""
    script = _load('2026_11_02_not_relevant_senders')
    with flask_app.app_context():
        rows = script.survey(script.find(('Nosuchsenderatall',)))
        assert rows[0]['leads'] == []
        assert script.plan_rules(rows) == []


def test_a_lead_with_a_quote_is_skipped(world):
    """One of the fifteen having a real enquiry is what the skip is for."""
    script = _load('2026_11_02_not_relevant_senders')
    with flask_app.app_context():
        from app.services import junk_delete as jd
        lead = _lead(company='Opterix', email='x@opterix.example',
                     stage='Quoted',
                     procam_vertical='Project Freight')
        db.session.commit()
        assert jd.blockers(lead), 'a Quoted lead is not junk'
        rows = script.survey(script.find(('Opterix',)))
        mine = rows[0]
        assert mine['eligible'] == []
        assert len(mine['refused']) == 1


def test_the_script_never_blocks_the_sender(world):
    script = _load('2026_11_02_not_relevant_senders')
    source = open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'scripts', '2026_11_02_not_relevant_senders.py')).read()
    assert 'block_sender=False' in source
    assert 'block_sender=True' not in source


# ══ the L&T alias ═══════════════════════════════════════════════════
def test_lntecc_cannot_reach_larsen_toubro_without_an_alias(world):
    """The answer to "why did it not link": there is no alias.

    `squash("Larsen & Toubro")` is 'larsentoubro' and `squash("Lntecc")`
    is 'lntecc'. Neither is a prefix of the other and neither contains
    the other, so nothing but an alias can connect them.
    """
    with flask_app.app_context():
        account = _plain('Larsen & Toubro')
        db.session.commit()
        assert m2.squash('Larsen & Toubro') == 'larsentoubro'
        assert m2.squash('Lntecc') == 'lntecc'
        index = m2._alias_index([account])
        found, _how = m2.looks_like_existing('Lntecc', '', index, {})
        assert found is None, 'without the alias there is nothing to match'

        account.aliases = ['LNT']
        db.session.commit()
        index = m2._alias_index([account])
        found, how = m2.looks_like_existing('Lntecc', '', index, {})
        assert found is not None and found.id == account.id
        assert 'LNT' in how


def test_the_alias_script_refuses_to_choose_between_two_accounts(world):
    script = _load('2026_11_02_lnt_alias')
    with flask_app.app_context():
        _plain('Larsen & Toubro')
        _plain('Larsen & Toubro Construction')
        db.session.commit()
        found = script.candidates()
        assert len(found) >= 2, \
            'both are candidates, and the script must not pick one'


def test_the_alias_is_reassigned_not_appended(world):
    """A JSON column does not notice a list being mutated in place."""
    source = open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'scripts', '2026_11_02_lnt_alias.py')).read()
    assert 'account.aliases = aliases' in source


def test_a_domain_seen_only_on_a_working_lead_is_not_added(world):
    """The correction that matters most in this script.

    The first version counted every domain it saw, including on leads it
    had just refused to touch — so a sender with one live quote behind
    it had its domain added, and the client who sent that quote would
    have had their next mail filed as junk.
    """
    script = _load('2026_11_02_not_relevant_senders')
    with flask_app.app_context():
        _lead(company='Promedia', email='sales@promedia-live.example',
              stage='Quoted', procam_vertical='Project Freight')
        db.session.commit()
        rows = script.survey(script.find(('Promedia',)))
        assert rows[0]['eligible'] == []
        assert len(rows[0]['refused']) == 1
        assert '*@promedia-live.example' not in [
            p for p, _w in script.plan_rules(rows)]
        # and the exclusion is reported, not silent
        held = script.held_back_rules(rows)
        assert '*@promedia-live.example' in [p for p, _n, _c in held]


# ══ what the live run of 2 Nov found ════════════════════════════════
def test_a_lead_at_new_is_not_past_new_opportunity(world):
    """The bug the live run exposed.

    `New` is the FIRST stage on the journey and `New Opportunity` the
    fourth. The gate read `stage != ELIGIBLE_STAGE`, which refused
    every brand-new lead — the exact population the junk module exists
    for. All fifteen named senders came back "it is at New, not New
    Opportunity" and nothing could be filed.
    """
    with flask_app.app_context():
        from app.services import junk_delete as jd
        from app.services import stage_map
        assert stage_map.position('New') < \
            stage_map.position('New Opportunity')
        lead = _lead(company='Brandnew Ltd', email='x@brandnew.example',
                     stage='New')
        db.session.commit()
        assert 'stage' not in dict(jd.blockers(lead))
        assert jd.eligible(lead) is True


def test_a_lead_past_new_opportunity_is_still_refused(world):
    with flask_app.app_context():
        from app.services import junk_delete as jd
        for stage in ('RFQ Generated', 'Quoted', 'Under Negotiation'):
            lead = _lead(company=f'Worked {stage}',
                         email=f'x@{stage.replace(" ", "")}.example',
                         stage=stage)
            db.session.commit()
            why = dict(jd.blockers(lead))
            assert 'worked' in why, stage
            assert 'past New Opportunity' in why['worked']


def test_a_closed_lead_is_refused_and_says_so(world):
    """Lost is terminal, and three of the live Promedia leads are Lost."""
    with flask_app.app_context():
        from app.services import junk_delete as jd
        lead = _lead(company='Closed Ltd', email='x@closedltd.example',
                     stage='Lost')
        db.session.commit()
        why = dict(jd.blockers(lead))
        assert 'closed' in why
        assert 'closed' in why['closed']


def test_sales_work_can_never_be_ignored(world):
    """No flag makes a quote untrue."""
    with flask_app.app_context():
        from app.services import junk_delete as jd
        lead = _lead(company='Quoted Ltd', email='x@quotedltd.example',
                     stage='New')
        db.session.commit()
        for key in jd.WORK_KEYS:
            with pytest.raises(ValueError) as caught:
                jd.mark_not_relevant(lead, reason='spam', actor='TEST',
                                     ignore=(key,), commit=False)
            assert 'cannot be ignored' in str(caught.value)


def test_a_note_can_be_overlooked_only_when_it_is_named(world):
    with flask_app.app_context():
        from app import LeadNote
        from app.services import junk_delete as jd
        lead = _lead(company='Noted Ltd', email='x@notedltd.example',
                     stage='New')
        db.session.add(LeadNote(lead_id=lead.id, note_text='junk',
                                author='EMP572012'))
        db.session.commit()
        assert 'note' in dict(jd.blockers(lead))
        with pytest.raises(ValueError):
            jd.mark_not_relevant(lead, reason='spam', actor='TEST',
                                 commit=False)
        out = jd.mark_not_relevant(lead, reason='spam', actor='TEST',
                                   ignore=('note',), commit=True)
        assert out
        assert db.session.get(Lead, lead.id).junk_deleted is True


def test_the_run_prints_what_the_note_says(world):
    """Nobody can judge "somebody wrote a note on it" without reading it."""
    script = _load('2026_11_02_not_relevant_senders')
    with flask_app.app_context():
        from app import LeadNote
        lead = _lead(company='Marineinsight', stage='New',
                     email='news@info.marineinsight.example')
        db.session.add(LeadNote(lead_id=lead.id, note_text='junk',
                                author='EMP572012'))
        db.session.commit()
        rows = script.survey(script.find(('Marineinsight',)))
        refused = rows[0]['refused']
        assert refused and 'note' in refused[0]['keys']
        assert 'junk' in refused[0]['notes']


def test_a_soft_blocker_still_yields_a_domain_rule(world):
    """A note is not a live enquiry, so the domain is still worth a rule."""
    script = _load('2026_11_02_not_relevant_senders')
    with flask_app.app_context():
        from app import LeadNote
        lead = _lead(company='Itemsecure', stage='New',
                     email='news@softblock.example')
        db.session.add(LeadNote(lead_id=lead.id, note_text='junk',
                                author='EMP572012'))
        db.session.commit()
        rows = script.survey(script.find(('Itemsecure',)))
        mine = [r for r in rows if r['name'] == 'Itemsecure'][0]
        assert mine['eligible'] == []
        assert '*@softblock.example' in [
            p for p, _w in script.plan_rules(rows)], \
            'a note is soft — the sender is still a sender'


def test_the_alias_script_names_the_account_the_leads_use(world):
    """Four candidates on live data, and no way to tell them apart.

    The one the business already uses is the one its leads point at.
    """
    script = _load('2026_11_02_lnt_alias')
    with flask_app.app_context():
        big = _plain('LARSEN & TOUBRO LIMITED')
        small = _plain('Larsen & Toubro Hydrocarbon')
        db.session.commit()
        for i in range(4):
            _lead(company='LARSEN & TOUBRO LIMITED',
                  email=f'a{i}@lt.example', company_id=big.id)
        _lead(company='Larsen & Toubro Hydrocarbon',
              email='b@lth.example', company_id=small.id)
        db.session.commit()
        counts = script.lead_counts([big.id, small.id])
        assert counts[big.id] == 4
        assert counts[small.id] == 1
