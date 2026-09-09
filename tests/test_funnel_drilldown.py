"""Clicking a funnel stage shows the records behind the number — §68, §71.

The property that matters: the drill-through list must always agree with
the figure on the bar. If "Quoted 182" opens a list of 174, the funnel has
stopped being trustworthy, which is worse than not being clickable.
"""
import os
import sys
import tempfile
from urllib.parse import quote

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'DrillTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'drill.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Lead, Opportunity, Employee = (_main.Company, _main.Lead,
                                        _main.Opportunity, _main.Employee)

_TPL = os.path.join(_ROOT, 'templates', 'funnel')


@pytest.fixture()
def pipeline():
    with flask_app.app_context():
        db.create_all()
        Opportunity.query.delete()
        Lead.query.delete()
        Company.query.delete()

        e = Employee.query.filter_by(emp_code='DRADM').first()
        if not e:
            e = Employee(emp_code='DRADM', name='Drill Admin')
            db.session.add(e)
        e.role, e.is_active, e.must_change_pw = 'admin', True, False
        e.is_super_admin, e.vertical = True, 'All'

        ids = []
        for n in range(8):
            c = Company(name=f'Drill Co {n}', is_active=True, city='Pune')
            db.session.add(c)
            db.session.flush()
            ids.append(c.id)
        for n in range(5):
            db.session.add(Lead(company=f'Drill Co {n}', company_id=ids[n],
                                stage='New', assigned_to='DRADM'))
        for n in range(3):
            db.session.add(Opportunity(
                opp_number=f'DR-{n}', company_id=ids[n],
                stage='Won' if n == 0 else 'RFQ',
                value_inr=1000, owner_emp_code='DRADM'))
        db.session.commit()
        return ids


def _c():
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='DRADM', name='Drill Admin', role='admin',
                 vertical='All')
    return c


def test_every_stage_list_matches_its_number(pipeline):
    """The whole point: the list behind a bar must equal the bar."""
    client = _c()
    stages = client.get('/api/funnel/sales').get_json()['stages']
    assert stages
    for s in stages:
        r = client.get('/api/funnel/sales/records?stage='
                       + quote(s['stage'])).get_json()
        assert r['ok'], r
        assert r['count'] == s['count'], (
            f'{s["stage"]}: chart says {s["count"]}, '
            f'drill-through returns {r["count"]}')


def test_records_carry_a_name_and_a_route(pipeline):
    r = _c().get('/api/funnel/sales/records?stage=Target%20Accounts').get_json()
    assert r['records']
    for rec in r['records']:
        assert rec['name']
        assert rec['route'].startswith('/companies/'), \
            'an account should lead to its Company 360'


def test_filters_apply_to_the_drill_through_too(pipeline):
    """A filtered chart with an unfiltered list would be a lie."""
    client = _c()
    chart = client.get(
        '/api/funnel/sales?vertical=Nonexistent').get_json()['stages']
    contacted = [s for s in chart
                 if s['stage'] == 'Contact Established'][0]
    drill = client.get('/api/funnel/sales/records'
                       '?stage=Contact%20Established'
                       '&vertical=Nonexistent').get_json()
    assert drill['count'] == contacted['count'] == 0


def test_unknown_stage_and_funnel_are_404(pipeline):
    assert _c().get(
        '/api/funnel/sales/records?stage=Nope').status_code == 404
    assert _c().get(
        '/api/funnel/nope/records?stage=Won').status_code == 404


def test_a_stage_is_required(pipeline):
    assert _c().get('/api/funnel/sales/records').status_code == 400


@pytest.mark.parametrize('which', ['account-development',
                                   'project-intelligence'])
def test_every_stage_of_every_funnel_drills(pipeline, which):
    """Not one stage per funnel — every one.

    The earlier version of this test checked `Target Account` and
    `Project Identified` and passed, while RFQ, Quote, Negotiation, Won
    and TMS Project all answered 404: only the early stages had an entry
    in the stage→raw-status maps the drill-through read. Asking the
    funnel itself which stages exist is what closes that gap for good.
    """
    client = _c()
    stages = client.get(f'/api/funnel/{which}').get_json()['stages']
    assert stages, f'{which} returned no stages'
    for s in stages:
        name = s['stage']
        r = client.get(f'/api/funnel/{which}/records?stage={quote(name)}')
        assert r.status_code == 200, \
            f'{which} / {name} → {r.status_code} (stage is not drillable)'
        body = r.get_json()
        assert body['ok']
        assert body['count'] == s['count'], (
            f'{which} / {name}: chart says {s["count"]}, '
            f'the list has {body["count"]}')


def test_every_drilled_record_can_be_opened(pipeline):
    """A row nobody can click is a dead end, so every record carries a
    name and a route into the portal."""
    client = _c()
    for which in ('sales', 'account-development', 'project-intelligence'):
        for s in client.get(f'/api/funnel/{which}').get_json()['stages']:
            recs = client.get(f'/api/funnel/{which}/records?stage='
                              + quote(s['stage'])).get_json()['records']
            for rec in recs:
                assert rec.get('name'), f'{which}/{s["stage"]}: unnamed record'
                assert (rec.get('route') or '').startswith('/'), \
                    f'{which}/{s["stage"]}: {rec!r} has no route'


# ── the stub is gone and the bars are clickable ──────────────────────
@pytest.mark.parametrize('name', ['account_development',
                                  'project_intelligence', 'sales'])
def test_stages_are_clickable(name):
    src = open(os.path.join(_TPL, f'{name}.html')).read()
    assert 'drill-through coming next iteration' not in src, \
        'the placeholder alert is still there'
    assert 'async function drill(' in src
    assert 'drillCard' in src


@pytest.mark.parametrize('name', ['account_development',
                                  'project_intelligence'])
def test_the_drill_reuses_the_charts_filters(name):
    """One filter reader, so the list cannot use different filters than
    the chart drawn above it."""
    src = open(os.path.join(_TPL, f'{name}.html')).read()
    assert 'function filterQuery()' in src
    assert src.count('filterQuery()') >= 2, \
        'loadData and drill must both use it'
