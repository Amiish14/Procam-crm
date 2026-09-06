"""Sales Funnel — §68-71.

"Each layer should visually communicate reduction." A funnel that widens
at the bottom is not a funnel, and the first version did exactly that:
layers were counted independently, so an account whose deal never touched
the unused RFQ and Quote modules vanished mid-funnel and reappeared at
Won with a negative drop-off.
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
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'FunnelTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'funnel.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Lead, Opportunity, Employee = (_main.Company, _main.Lead,
                                        _main.Opportunity, _main.Employee)


@pytest.fixture()
def pipeline():
    """Ten accounts, narrowing: 10 targets → 6 contacted → 4 opps → 1 won."""
    with flask_app.app_context():
        db.create_all()
        Opportunity.query.delete()
        Lead.query.delete()
        Company.query.delete()

        e = Employee.query.filter_by(emp_code='FNADM').first()
        if not e:
            e = Employee(emp_code='FNADM', name='Funnel Admin')
            db.session.add(e)
        e.role, e.is_active, e.must_change_pw = 'admin', True, False
        e.is_super_admin, e.vertical = True, 'All'

        ids = []
        for n in range(10):
            c = Company(name=f'Account {n}', is_active=True)
            db.session.add(c)
            db.session.flush()
            ids.append(c.id)

        for n in range(6):
            db.session.add(Lead(company=f'Account {n}', company_id=ids[n],
                                stage='New', assigned_to='FNADM'))
        for n in range(4):
            db.session.add(Opportunity(
                opp_number=f'F-{n}', company_id=ids[n],
                stage='Won' if n == 0 else 'Negotiation' if n == 1 else 'RFQ',
                value_inr=100000, owner_emp_code='FNADM'))
        db.session.commit()
        return ids


def _c():
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='FNADM', name='Funnel Admin', role='admin',
                 vertical='All')
    return c


def _stages():
    return _c().get('/api/funnel/sales').get_json()['stages']


def test_the_funnel_never_widens(pipeline):
    """The property that makes it a funnel."""
    previous = None
    for stage in _stages():
        if previous is not None:
            assert stage['count'] <= previous, (
                f'{stage["stage"]} has {stage["count"]}, more than the '
                f'{previous} above it — the funnel widens')
        previous = stage['count']


def test_drop_off_is_never_negative(pipeline):
    for stage in _stages():
        if stage['drop_off'] is not None:
            assert stage['drop_off'] >= 0


def test_layers_reflect_the_data(pipeline):
    by_name = {s['stage']: s for s in _stages()}
    assert by_name['Target Accounts']['count'] == 10
    assert by_name['Contact Established']['count'] == 6
    assert by_name['Opportunities']['count'] == 4
    assert by_name['Won']['count'] == 1


def test_a_won_account_counts_at_every_stage_above(pipeline):
    """An account that reached Won passed everything above it, even where
    the intervening modules hold no records."""
    by_name = {s['stage']: s for s in _stages()}
    assert by_name['Negotiation']['count'] >= by_name['Won']['count']
    assert by_name['RFQs']['count'] >= by_name['Negotiation']['count']


def test_conversion_is_reported_between_layers(pipeline):
    stages = _stages()
    assert stages[0]['conversion_pct_from_previous'] is None
    contacted = stages[1]
    assert contacted['conversion_pct_from_previous'] == 60.0
    assert contacted['drop_off'] == 4


def test_pct_of_top_is_reported(pipeline):
    by_name = {s['stage']: s for s in _stages()}
    assert by_name['Target Accounts']['pct_of_top'] == 100.0
    assert by_name['Opportunities']['pct_of_top'] == 40.0


def test_filters_narrow_the_funnel(pipeline):
    body = _c().get('/api/funnel/sales?vertical=Nonexistent').get_json()
    assert body['ok']
    contacted = [s for s in body['stages']
                 if s['stage'] == 'Contact Established'][0]
    assert contacted['count'] == 0, 'an unmatched filter should empty it'


def test_page_renders(pipeline):
    assert _c().get('/funnels/sales').status_code == 200


def test_anonymous_is_refused(pipeline):
    assert flask_app.test_client().get(
        '/api/funnel/sales').status_code in (302, 401)
