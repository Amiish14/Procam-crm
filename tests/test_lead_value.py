"""Lead commercial values: opportunity value, quote, currency.

Values are stored in rupees (and as entered, with the rate used), shown in
lakh or crore, never millions. Re-quoting keeps the version it replaces.
A later exchange-rate change does not move a saved figure.
"""
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from decimal import Decimal

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'ValueTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'value.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Lead, Employee = _main.Lead, _main.Employee
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.services import lead_value as lv                 # noqa: E402
from app.models.master_data import MasterItem             # noqa: E402

ADMIN = 'VALADM'


@pytest.fixture()
def lead_id():
    with flask_app.app_context():
        db.create_all()
        MasterItem.query.filter_by(list_key=lv.FX_LIST).delete()
        e = Employee.query.filter_by(emp_code=ADMIN).first()
        if not e:
            e = Employee(emp_code=ADMIN, name='Value Admin')
            db.session.add(e)
        e.role, e.is_active, e.must_change_pw = 'admin', True, False
        e.is_super_admin, e.session_version = True, 0
        l = Lead(company='Drewes Group', stage='Quoted', assigned_to=ADMIN)
        db.session.add(l)
        db.session.commit()
        return l.id


def _client():
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=ADMIN, name='Value Admin', role='admin',
                 vertical='All', sv=0)
    return c


def _put(lid, body):
    return _client().put(f'/api/leads/{lid}', data=json.dumps(body),
                         content_type='application/json')


def _lead(lid):
    with flask_app.app_context():
        l = db.session.get(Lead, lid)
        db.session.expunge(l)
        return l


def test_opportunity_value_in_rupees_mirrors_the_old_field(lead_id):
    r = _put(lead_id, {'opportunity_value': '12,50,000', 'value_basis': 'client_budget'})
    assert r.status_code == 200, r.get_json()
    l = _lead(lead_id)
    assert l.estimated_value_inr == Decimal('1250000.00')
    assert l.opportunity_value_num == Decimal('1250000.00')
    assert l.cost_million == pytest.approx(1.25)
    assert l.value_basis == 'client_budget'


def test_quote_block_saves_and_the_list_value_prefers_the_quote(lead_id):
    _put(lead_id, {'opportunity_value': 1000000})
    today = date.today()
    r = _put(lead_id, {'quote_no': 'RFQ-DLI-26-0061', 'quote_value': 900000,
                       'quote_date': str(today), 'quote_validity_days': 15,
                       'quote_cost': 720000})
    assert r.status_code == 200, r.get_json()
    d = _client().get(f'/api/leads/{lead_id}').get_json()
    assert d['value_inr'] == 900000 and d['value_source'] == 'quote'
    assert d['quote_validity_date'] == str(today + timedelta(days=15))
    assert d['quote_margin'] == 180000 and d['quote_margin_pct'] == 20.0
    assert d['quote_revision'] == 0 and d['quote_revisions'] == []
    assert d['quote_recorded_by'] == ADMIN


def test_filling_a_blank_is_not_a_requote_but_replacing_a_value_is(lead_id):
    today = str(date.today())
    _put(lead_id, {'quote_value': 500000})
    _put(lead_id, {'quote_value': 500000, 'quote_date': today})
    assert _lead(lead_id).quote_revision == 0
    _put(lead_id, {'quote_value': 450000, 'quote_date': today})
    l = _lead(lead_id)
    assert l.quote_revision == 1 and l.quoted_amount_inr == Decimal('450000.00')
    (old,) = l.quote_revisions
    assert old['quote_value'] == 500000 and old['revision'] == 0
    assert old['quote_date'] == today and old['recorded_by'] == ADMIN


def test_saving_unchanged_values_changes_nothing(lead_id):
    body = {'currency': 'INR', 'opportunity_value': 800000, 'value_basis': '',
            'quote_no': 'Q1', 'quote_value': 700000,
            'quote_date': str(date.today()), 'quote_validity_date': '',
            'quote_cost': ''}
    _put(lead_id, body)
    _put(lead_id, body)
    assert _lead(lead_id).quote_revision == 0


def test_a_legacy_quoted_amount_saved_unchanged_is_not_a_revision(lead_id):
    with flask_app.app_context():
        l = db.session.get(Lead, lead_id)
        l.quoted_amount_inr, l.estimated_value_inr = Decimal('300000'), Decimal('400000')
        db.session.commit()
    _put(lead_id, {'currency': 'INR', 'opportunity_value': 400000,
                   'quote_value': 300000, 'quote_no': '', 'quote_date': '',
                   'quote_validity_date': '', 'quote_cost': ''})
    l = _lead(lead_id)
    assert l.quote_revision in (0, None) and not l.quote_revisions


def test_foreign_currency_needs_a_rate_and_keeps_the_rate_it_was_saved_at(lead_id):
    r = _put(lead_id, {'currency': 'USD', 'quote_value': 10000})
    assert r.status_code == 400 and 'exchange rate' in r.get_json()['error']
    assert _lead(lead_id).value_currency in (None, 'INR')

    c = _client()
    r = c.put('/api/master/fx-rates/USD', data=json.dumps({'inr_per_unit': 83.5}),
              content_type='application/json')
    assert r.status_code == 200, r.get_json()
    _put(lead_id, {'currency': 'USD', 'quote_value': 10000, 'opportunity_value': 12000})
    l = _lead(lead_id)
    assert l.quoted_amount_inr == Decimal('835000.00')
    assert l.estimated_value_inr == Decimal('1002000.00')
    assert l.quote_fx_rate == Decimal('83.5000')

    c.put('/api/master/fx-rates/USD', data=json.dumps({'inr_per_unit': 90}),
          content_type='application/json')
    _put(lead_id, {'currency': 'USD', 'quote_value': 10000, 'opportunity_value': 12000})
    l = _lead(lead_id)
    assert l.quoted_amount_inr == Decimal('835000.00')
    assert l.estimated_value_inr == Decimal('1002000.00')

    r = _put(lead_id, {'cost': 2})
    assert r.status_code == 400 and 'USD' in r.get_json()['error']


def test_only_master_data_admins_set_rates(lead_id):
    with flask_app.app_context():
        e = Employee.query.filter_by(emp_code='VALREP').first() or \
            Employee(emp_code='VALREP', name='Rep')
        e.role, e.is_active, e.is_super_admin, e.session_version = 'user', True, False, 0
        db.session.add(e); db.session.commit()
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='VALREP', name='Rep', role='user', sv=0)
    r = c.put('/api/master/fx-rates/USD', data=json.dumps({'inr_per_unit': 1}),
              content_type='application/json')
    assert r.status_code == 403
    assert c.get('/api/master/fx-rates').get_json()['ok'] is True


@pytest.mark.parametrize('body,message', [
    ({'quote_value': -5}, 'negative'),
    ({'opportunity_value': 'lots'}, 'number'),
    ({'currency': 'GBP'}, 'Currency'),
    ({'value_basis': 'guess'}, 'basis'),
    ({'quote_date': '2026-09-10', 'quote_validity_date': '2026-09-01'}, 'before'),
    ({'quote_validity_days': 10}, 'quote date'),
    ({'quote_date': str(date.today() + timedelta(days=10))}, 'future'),
])
def test_bad_input_is_refused_and_nothing_is_saved(lead_id, body, message):
    r = _put(lead_id, body)
    assert r.status_code == 400 and message in r.get_json()['error'], r.get_json()
    l = _lead(lead_id)
    assert l.quoted_amount_inr is None and l.quote_date is None
    assert l.estimated_value_inr is None


def test_legacy_cost_in_millions_becomes_rupees(lead_id):
    _put(lead_id, {'cost': 4.5})
    l = _lead(lead_id)
    assert l.estimated_value_inr == Decimal('4500000.00') and l.cost_million == 4.5


def test_won_decision_records_the_business_value(lead_id):
    r = _client().post(f'/api/leads/{lead_id}/decision',
                       data=json.dumps({'outcome': 'Won', 'value': 2500000}),
                       content_type='application/json')
    assert r.status_code == 200, r.get_json()
    l = _lead(lead_id)
    assert l.stage == 'Won' and l.estimated_value_inr == Decimal('2500000.00')
    assert l.value_basis == 'firm'


def test_dashboard_sums_rupees_from_quote_then_opportunity_then_legacy():
    with flask_app.app_context():
        db.create_all()
        rows = [Lead(company='V1', stage='Won', quoted_amount_inr=Decimal('900000'),
                     estimated_value_inr=Decimal('1000000')),
                Lead(company='V2', stage='Won', estimated_value_inr=Decimal('2000000')),
                Lead(company='V3', stage='Won', cost_million=0.5)]
        cols = _main._SUMMARY_COLUMNS
    with flask_app.test_request_context('/'):
        got = _main._dashboard_summary_payload(
            [type('R', (), {c: getattr(r, c, None) for c in cols})() for r in rows],
            [], date.today())
    assert got['kpis']['won_value_inr'] == 3400000
    assert got['kpis']['won_value_m'] == pytest.approx(3.4)


def test_display_is_lakh_below_a_crore_and_crore_above():
    assert lv.format_inr(Decimal('1250000')) == '₹ 12.5 L'
    assert lv.format_inr(Decimal('35000000')) == '₹ 3.50 Cr'
    assert lv.format_inr(None) == '—'


def test_quote_ageing_flags_a_lapsed_quote_only_while_it_is_open():
    l = Lead(stage='Quoted', quote_date=date.today() - timedelta(days=20),
             quote_validity_date=date.today() - timedelta(days=5))
    a = lv.ageing(l)
    assert a['days_since_quote'] == 20 and a['validity_days_left'] == -5
    assert a['expired'] is True
    l.stage = 'Won'
    assert lv.ageing(l)['expired'] is False


def test_stage_warnings():
    l = Lead(stage='Quoted')
    assert lv.warnings_for_stage(l, 'Quoted') == ['quote_value', 'quote_date']
    assert lv.warnings_for_stage(l, 'Won') == ['value']
    l.estimated_value_inr = Decimal('1')
    assert lv.warnings_for_stage(l, 'Won') == []


def test_an_amount_read_from_email_is_a_suggestion_not_the_quote():
    import email_ingest.single_message as sm
    import logging
    l = Lead(company='S', opp_notes=None)
    sm._stamp_quote(l, None, {'amount': Decimal('450000'), 'currency': 'INR'},
                    logging.getLogger('t'))
    assert l.quoted_amount_inr is None
    assert lv.quote_suggestion(l)['amount'] == '450000'


def test_data_quality_finds_quoted_without_quote_won_without_value_and_lapsed():
    from app.data_quality import service as dq
    with flask_app.app_context():
        db.create_all()
        before = {k: dq._FUNCTIONS[k](None)[0] for k in
                  ('quoted_without_quote', 'won_leads_no_value', 'quote_past_validity')}
        db.session.add_all([
            Lead(company='DQ quoted bare', stage='Quoted'),
            Lead(company='DQ quoted full', stage='Quoted', quoted_amount_inr=Decimal('1'),
                 quote_date=date.today()),
            Lead(company='DQ won bare', stage='Won'),
            Lead(company='DQ won legacy', stage='Won', cost_million=1.0),
            Lead(company='DQ lapsed', stage='Under Negotiation', quoted_amount_inr=Decimal('5'),
                 quote_date=date.today() - timedelta(days=40),
                 quote_validity_date=date.today() - timedelta(days=10)),
            Lead(company='DQ lapsed but won', stage='Won', quoted_amount_inr=Decimal('5'),
                 quote_validity_date=date.today() - timedelta(days=10)),
        ])
        db.session.commit()
        after = {k: dq._FUNCTIONS[k](None)[0] for k in before}
    assert after['quoted_without_quote'] - before['quoted_without_quote'] == 1
    assert after['won_leads_no_value'] - before['won_leads_no_value'] == 1
    assert after['quote_past_validity'] - before['quote_past_validity'] == 1
