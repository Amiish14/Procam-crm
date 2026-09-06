"""Company 360 — §12-16, §87.

§87's acceptance test: clicking the same company from anywhere must reach
the SAME record. §6: Customers and Competitors are filtered views of the
one master, not separate lists.
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
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'C360TestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'c360.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Lead, Contact = _main.Company, _main.Lead, _main.Contact
Opportunity, Employee = _main.Opportunity, _main.Employee
flask_app.config['WTF_CSRF_ENABLED'] = False

from presales.models import AccountRelationshipTag           # noqa: E402
from app.master_data import service as md                    # noqa: E402
from app.company360 import service as c360                   # noqa: E402


@pytest.fixture(scope='module')
def world():
    with flask_app.app_context():
        db.create_all()
        md.ensure_lists()
        for label in ('Customer', 'Competitor', 'Vendor', 'Overseas Partner'):
            md.add_item('relationship', label, label)

        e = Employee.query.filter_by(emp_code='C360ADM').first()
        if not e:
            e = Employee(emp_code='C360ADM', name='C360 Admin')
            db.session.add(e)
        e.role, e.is_active, e.must_change_pw = 'admin', True, False
        e.is_super_admin, e.vertical = True, 'All'

        acme = Company(name='Acme Heavy Industries', industry='Power',
                       city='Pune', country='India', is_active=True,
                       pic_emp_code='C360ADM')
        quiet = Company(name='Quiet Corp', is_active=True)
        db.session.add_all([acme, quiet])
        db.session.flush()

        # One company, three classifications — the §4 principle.
        for tag in ('Customer', 'Vendor', 'Competitor'):
            db.session.add(AccountRelationshipTag(account_id=acme.id, tag=tag))

        db.session.add(Contact(name='Rita Shah', company='Acme Heavy',
                               company_id=acme.id, designation='Head of Ops'))
        for n in range(2):
            db.session.add(Lead(company='Acme Heavy Industries',
                                company_id=acme.id, project=f'Move {n}',
                                stage='New'))
        db.session.add(Opportunity(opp_number='A-1', company_id=acme.id,
                                   stage='Won', value_inr=500000))
        db.session.add(Opportunity(opp_number='A-2', company_id=acme.id,
                                   stage='Lost', value_inr=100000))
        db.session.commit()
        return {'acme': acme.id, 'quiet': quiet.id}


def _c():
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='C360ADM', name='C360 Admin', role='admin',
                 vertical='All')
    return c


# ── §4/§5: one record, several classifications ───────────────────────
def test_one_company_holds_many_classifications(world):
    with flask_app.app_context():
        tags = c360.classifications(world['acme'])
    assert set(tags) == {'Customer', 'Vendor', 'Competitor'}, \
        'a company must hold several relationship types at once'


def test_classifications_are_validated_against_master_data(world):
    """A typo must not silently create a new relationship type."""
    r = _c().post(f'/api/companies/{world["acme"]}/classifications',
                  data=json.dumps({'classifications': ['Custmer']}),
                  content_type='application/json')
    assert r.status_code == 400
    assert 'Master Data' in r.get_json()['error']


def test_classifications_can_be_replaced(world):
    cl = _c()
    r = cl.post(f'/api/companies/{world["acme"]}/classifications',
                data=json.dumps({'classifications':
                                 ['Customer', 'Overseas Partner']}),
                content_type='application/json')
    assert r.status_code == 200
    assert set(r.get_json()['classifications']) == {'Customer',
                                                    'Overseas Partner'}
    # put it back for the other tests
    cl.post(f'/api/companies/{world["acme"]}/classifications',
            data=json.dumps({'classifications':
                             ['Customer', 'Vendor', 'Competitor']}),
            content_type='application/json')


# ── §14: the numbers come from the linked records ────────────────────
def test_kpis_are_computed_from_links(world):
    with flask_app.app_context():
        company = Company.query.get(world['acme'])
        k = c360.kpis(company)
    assert k['leads'] == 2
    assert k['contacts'] == 1
    assert k['opportunities'] == 2
    assert k['won'] == 1 and k['lost'] == 1
    assert k['won_value'] == 500000
    assert k['win_rate'] == 50.0


def test_a_company_with_no_history_still_renders(world):
    """The empty state matters: most companies have little recorded."""
    r = _c().get(f'/companies/{world["quiet"]}')
    assert r.status_code == 200
    assert 'Quiet Corp' in r.get_data(as_text=True)


# ── §87: every path reaches the same record ──────────────────────────
def test_every_route_resolves_to_the_same_company(world):
    cl = _c()
    page = cl.get(f'/companies/{world["acme"]}')
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert 'Acme Heavy Industries' in body

    api = cl.get(f'/api/companies/{world["acme"]}/360').get_json()
    assert api['ok']
    assert api['header']['id'] == world['acme']
    assert api['header']['name'] == 'Acme Heavy Industries'


def test_a_merged_company_redirects_to_its_survivor(world):
    """Old links and bookmarks must survive de-duplication (§87)."""
    with flask_app.app_context():
        dead = Company(
            name=f'Acme Heavy Ind. [merged into #{world["acme"]}]',
            is_active=False)
        db.session.add(dead)
        db.session.commit()
        dead_id = dead.id

    r = _c().get(f'/companies/{dead_id}')
    assert r.status_code == 302
    assert r.headers['Location'].endswith(f'/companies/{world["acme"]}')


def test_unknown_company_is_a_clean_404(world):
    r = _c().get('/companies/999999')
    assert r.status_code == 404
    assert 'No company with id' in r.get_data(as_text=True)


# ── §6: filtered views, not separate masters ─────────────────────────
def test_filtered_view_returns_the_master_filtered(world):
    body = _c().get('/companies?relationship=Customer').get_data(as_text=True)
    assert 'Acme Heavy Industries' in body
    assert 'Quiet Corp' not in body, \
        'the filter must exclude companies without that classification'


def test_named_routes_are_aliases_of_the_master(world):
    r = _c().get('/customers')
    assert r.status_code == 302
    assert 'relationship=Customer' in r.headers['Location']


def test_anonymous_users_are_sent_to_login(world):
    r = flask_app.test_client().get(f'/companies/{world["acme"]}')
    assert r.status_code == 302
