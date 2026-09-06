"""Competitor 360 — §2, §19-21.

§19 is unambiguous: "Competitor = Company classification." The old
competitor_masters table was a second company master, which §4 forbids.
These tests pin the new arrangement: one record, classified, with the
competitive history hanging off it.
"""
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'CompTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'comp.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Opportunity, Employee = _main.Company, _main.Opportunity, _main.Employee

from presales.models import AccountRelationshipTag         # noqa: E402
from app.models.competitor import (OpportunityCompetitor,   # noqa: E402
                                   CompetitorIntelligence)
from app.master_data import service as md                   # noqa: E402
from app.company360 import service as c360                   # noqa: E402


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        md.ensure_lists()
        for label in ('Customer', 'Competitor'):
            md.add_item('relationship', label, label)
        CompetitorIntelligence.query.delete()
        OpportunityCompetitor.query.delete()
        AccountRelationshipTag.query.delete()
        Opportunity.query.delete()
        Company.query.delete()

        e = Employee.query.filter_by(emp_code='CPADM').first()
        if not e:
            e = Employee(emp_code='CPADM', name='Comp Admin')
            db.session.add(e)
        e.role, e.is_active, e.must_change_pw = 'admin', True, False
        e.is_super_admin, e.vertical = True, 'All'

        rival = Company(name='Rival Logistics', is_active=True)
        customer = Company(name='Big Customer', is_active=True)
        plain = Company(name='Just A Customer', is_active=True)
        db.session.add_all([rival, customer, plain])
        db.session.flush()

        db.session.add(AccountRelationshipTag(account_id=rival.id,
                                              tag='Competitor'))
        db.session.add(AccountRelationshipTag(account_id=plain.id,
                                              tag='Customer'))

        won = Opportunity(opp_number='C-WON', company_id=customer.id,
                          stage='Won', value_inr=500000)
        lost = Opportunity(opp_number='C-LOST', company_id=customer.id,
                           stage='Lost', value_inr=400000)
        db.session.add_all([won, lost])
        db.session.flush()

        db.session.add(OpportunityCompetitor(
            company_id=rival.id, opportunity_id=won.id, status='Confirmed'))
        db.session.add(OpportunityCompetitor(
            company_id=rival.id, opportunity_id=lost.id, status='Lost To',
            quoted_price=380000, price_source='customer feedback'))
        db.session.add(CompetitorIntelligence(
            company_id=rival.id, event_type='Tender Result',
            summary='Won the Kandla tender on price',
            source='customer', added_by_id='CPADM'))
        db.session.commit()
        return {'rival': rival.id, 'customer': customer.id,
                'plain': plain.id}


def _c():
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='CPADM', name='Comp Admin', role='admin',
                 vertical='All')
    return c


# ── §19: one record, classified ──────────────────────────────────────
def test_a_competitor_is_a_classified_company(world):
    with flask_app.app_context():
        assert 'Competitor' in c360.classifications(world['rival'])


def test_the_profile_appears_only_for_a_classified_competitor(world):
    with flask_app.app_context():
        assert c360.competitor_profile(world['rival']) is not None
        assert c360.competitor_profile(world['plain']) is None, \
            'a plain customer must not show a competitor section'


def test_a_company_can_be_customer_and_competitor_at_once(world):
    """§4 — one record, several classifications."""
    with flask_app.app_context():
        db.session.add(AccountRelationshipTag(account_id=world['rival'],
                                              tag='Customer'))
        db.session.commit()
        tags = c360.classifications(world['rival'])
        assert set(tags) == {'Competitor', 'Customer'}
        assert Company.query.filter(
            Company.name == 'Rival Logistics').count() == 1, \
            'the company was duplicated instead of reclassified'


# ── §19: the competitive record ──────────────────────────────────────
def test_win_loss_against_is_computed(world):
    with flask_app.app_context():
        p = c360.competitor_profile(world['rival'])
    assert p['encounter_count'] == 2
    assert p['won_against'] == 1
    assert p['lost_against'] == 1
    assert p['win_rate_against'] == 50.0


def test_known_pricing_is_captured(world):
    with flask_app.app_context():
        p = c360.competitor_profile(world['rival'])
    assert len(p['prices']) == 1
    price = p['prices'][0]
    assert price['their_price'] == 380000
    assert price['our_price'] == 400000
    assert price['source'] == 'customer feedback'


def test_customer_overlap_is_listed(world):
    with flask_app.app_context():
        p = c360.competitor_profile(world['rival'])
    assert [c['name'] for c in p['customer_overlap']] == ['Big Customer']


def test_intelligence_timeline_is_returned(world):
    with flask_app.app_context():
        p = c360.competitor_profile(world['rival'])
    assert len(p['intelligence']) == 1
    assert p['intelligence'][0]['type'] == 'Tender Result'


# ── §6/§87: one route, one record ────────────────────────────────────
def test_the_competitor_list_is_a_filtered_view(world):
    r = _c().get('/competitors')
    assert r.status_code == 302
    assert 'relationship=Competitor' in r.headers['Location']


def test_a_competitor_opens_the_same_company_360(world):
    body = _c().get(f'/companies/{world["rival"]}').get_data(as_text=True)
    assert 'Rival Logistics' in body
    assert 'Competitor 360' in body
    assert 'Won against' in body


def test_a_non_competitor_page_has_no_competitor_section(world):
    body = _c().get(f'/companies/{world["plain"]}').get_data(as_text=True)
    assert 'Competitor 360' not in body


# ── §21: several competitors per opportunity ─────────────────────────
def test_an_opportunity_can_carry_several_competitors(world):
    with flask_app.app_context():
        second = Company(name='Another Rival', is_active=True)
        db.session.add(second)
        db.session.flush()
        db.session.add(AccountRelationshipTag(account_id=second.id,
                                              tag='Competitor'))
        opp = Opportunity.query.filter_by(opp_number='C-WON').first()
        db.session.add(OpportunityCompetitor(
            company_id=second.id, opportunity_id=opp.id, status='Likely'))
        db.session.commit()

        rows = OpportunityCompetitor.query.filter_by(
            opportunity_id=opp.id).all()
        assert len(rows) == 2, \
            'an opportunity must hold more than one competitor'
        assert {r.status for r in rows} == {'Confirmed', 'Likely'}
