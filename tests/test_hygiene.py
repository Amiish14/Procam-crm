"""The CRM Hygiene Score: what it is worth, what it deducts and why.

The score is computed from the Data Quality checks, so these tests are
also the record of which checks make up each factor and what each factor
costs. The weights asserted here are the ones published in
docs/operations/HYGIENE_AND_ESCALATION.md: change one and this fails,
which is the point.
"""
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'hygiene-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'HygieneTest12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'hygiene.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Lead, Employee, Company = _main.Lead, _main.Employee, _main.Company
LeadActivity, Opportunity = _main.LeadActivity, _main.Opportunity
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.access import scope as sc_mod                    # noqa: E402
from app.models.access import AccessProfile, DataScope    # noqa: E402
from app.services import hygiene                          # noqa: E402

try:
    from app.hygiene.routes import bp as hygiene_bp
    if 'hygiene' not in flask_app.blueprints:
        flask_app.register_blueprint(hygiene_bp)
except Exception as exc:                                  # pragma: no cover
    raise AssertionError(f'the hygiene blueprint will not load: {exc}')

REP, OTHER, HEAD = 'HYREP', 'HYOTHER', 'HYHEAD'
TODAY = date.today()
TAG = 'HY '


def _emp(code, name, vertical='Hygiene Vertical', head=False, scope=None):
    e = Employee.query.filter_by(emp_code=code).first() or Employee(emp_code=code)
    e.name, e.vertical, e.is_active, e.must_change_pw = name, vertical, True, False
    e.role, e.is_super_admin, e.is_vertical_head = 'user', False, head
    e.session_version = 0
    db.session.add(e)
    db.session.flush()
    AccessProfile.query.filter_by(emp_code=code).delete()
    if scope:
        db.session.add(AccessProfile(emp_code=code, data_scope=scope))
    return e


def _lead(company, owner, **kw):
    """A lead with nothing wrong with it unless the caller says so."""
    fields = dict(
        company=TAG + company, assigned_to=owner, assigned_name=owner,
        stage='Business Discussion', procam_vertical='Hygiene Vertical',
        created_at=datetime.utcnow() - timedelta(days=2),
        followup_date=TODAY + timedelta(days=5))
    fields.update(kw)
    row = Lead(**fields)
    db.session.add(row)
    return row


def _contacted(lead, days_ago=0):
    db.session.add(LeadActivity(
        lead_id=lead.id, kind='call', subject='spoke',
        occurred_at=datetime.utcnow() - timedelta(days=days_ago),
        performed_by=lead.assigned_to))


def _wipe():
    """Only this module's rows: the suite shares one database."""
    from app.models.notification import Notification
    mine = [l.id for l in Lead.query.filter(Lead.company.like(TAG + '%'))]
    if mine:
        LeadActivity.query.filter(LeadActivity.lead_id.in_(mine)).delete(
            synchronize_session=False)
        Lead.query.filter(Lead.id.in_(mine)).delete(synchronize_session=False)
    Opportunity.query.filter(Opportunity.opp_number.like(TAG + '%')).delete(
        synchronize_session=False)
    Company.query.filter(Company.name.like(TAG + '%')).delete(
        synchronize_session=False)
    Notification.query.filter(Notification.user_id.in_(
        [REP, OTHER, HEAD])).delete(synchronize_session=False)


@pytest.fixture()
def clean():
    """A world in which this module's people own nothing wrong.

    Every test then breaks exactly one thing, so a deduction can be
    attributed to the factor under test and nothing else.
    """
    with flask_app.app_context():
        db.create_all()
        _wipe()
        _emp(REP, 'Hygiene Rep', scope=DataScope.OWN)
        _emp(OTHER, 'Another Rep', scope=DataScope.OWN)
        _emp(HEAD, 'Vertical Head', head=True, scope=DataScope.VERTICAL)
        db.session.flush()
        good = _lead('Spotless Ltd', REP)
        db.session.flush()
        _contacted(good, days_ago=0)
        db.session.commit()
        yield {'good': good.id}
        _wipe()
        db.session.commit()


def _score(code=REP, **kw):
    with flask_app.app_context():
        return hygiene.score(sc_mod.for_employee(code), **kw)


def _factor(data, key):
    return next(f for f in data['factors'] if f['key'] == key)


def _client(code):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role='user',
                 vertical='Hygiene Vertical', sv=0)
    return c


# ── the shape of the score ───────────────────────────────────────────
def test_the_weights_total_one_hundred_and_are_the_published_ones():
    assert sum(hygiene.weights().values()) == 100
    assert hygiene.weights() == {
        'ownership': 20, 'next_action': 12, 'followup_completion': 12,
        'overdue_updates': 12, 'rfq_quotation': 12, 'close_date': 8,
        'lost_reason': 4, 'won_handover': 12, 'account_contact': 8}


def test_a_perfect_dataset_scores_one_hundred(clean):
    data = _score()
    assert data['score'] == 100
    assert data['band'] == 'good'
    assert all(f['lost'] == 0 for f in data['factors']), [
        (f['key'], f['lost'], f['count']) for f in data['factors']
        if f['lost']]


def test_every_factor_says_what_it_is_worth_and_where_to_look(clean):
    for f in _score()['factors']:
        assert f['key'] and f['label'] and f['why']
        assert f['weight'] > 0
        assert f['route'].startswith('/hygiene/factor/')
        # The checks it is computed from are named, not implied.
        assert f['checks'] and all(c['label'] for c in f['checks'])


def test_the_score_is_built_from_the_data_quality_checks_not_new_rules():
    src = open(os.path.join(_ROOT, 'app', 'services', 'hygiene.py')).read()
    assert 'from app.data_quality import service as dq' in src
    assert 'from app.data_quality import definitions as defs' in src
    # No second opinion about what "stale" or "overdue" means.
    assert 'timedelta(days=' not in src
    from app.data_quality import service as dq
    known = {c.key for c in (dq._coerce(e) for e in dq.CHECKS)}
    for f in hygiene.FACTORS:
        for key in f['checks']:
            assert key in known, key


def test_the_bands_are_good_watch_and_poor():
    assert hygiene.band_for(100) == 'good'
    assert hygiene.band_for(hygiene.GOOD_AT) == 'good'
    assert hygiene.band_for(hygiene.GOOD_AT - 0.1) == 'watch'
    assert hygiene.band_for(hygiene.WATCH_AT) == 'watch'
    assert hygiene.band_for(hygiene.WATCH_AT - 0.1) == 'poor'


# ── each factor deducts exactly its weight, and names its records ────
def _assert_full_deduction(key, data):
    f = _factor(data, key)
    weight = hygiene.BY_KEY[key]['weight']
    assert f['population'] > 0, f'{key} has nothing to measure against'
    assert f['count'] >= f['population'], (key, f['count'], f['population'])
    assert f['lost'] == weight, (key, f['lost'], weight)
    assert data['score'] == pytest.approx(100 - weight, abs=0.05)
    # A deduction that cannot name its records is a number nobody can
    # argue with, which is the one thing this score must never be.
    assert f['records'], key
    assert all(r['name'] for r in f['records'])
    assert any(r['route'] for r in f['records'])
    return f


def test_a_lead_with_no_next_action_costs_exactly_the_next_action_weight(clean):
    with flask_app.app_context():
        for row in Lead.query.filter(Lead.company.like(TAG + '%')).all():
            row.followup_date = None
        db.session.commit()
    _assert_full_deduction('next_action', _score())


def test_an_uncontacted_lead_costs_exactly_the_follow_up_weight(clean):
    with flask_app.app_context():
        old = datetime.utcnow() - timedelta(days=90)
        for row in Lead.query.filter(Lead.company.like(TAG + '%')).all():
            row.created_at = old
        LeadActivity.query.filter(LeadActivity.lead_id.in_(
            [l.id for l in Lead.query.filter(
                Lead.company.like(TAG + '%')).all()])).delete(
            synchronize_session=False)
        db.session.commit()
    _assert_full_deduction('followup_completion', _score())


def test_an_unowned_lead_costs_the_ownership_weight_for_a_company_viewer(clean):
    """Ownership is measured company-wide, because an unowned record is
    invisible to a scoped viewer by the Access Matrix's own design."""
    with flask_app.app_context():
        for row in Lead.query.filter(Lead.company.like(TAG + '%')).all():
            row.assigned_to = None
            row.assigned_name = None
        db.session.commit()
        sc = hygiene.dq.system_scope()
        data = hygiene.score(sc)
    f = _factor(data, 'ownership')
    assert f['count'] >= 1 and f['lost'] > 0
    assert f['records'] and any(TAG in (r['name'] or '') for r in f['records'])
    assert 'unowned_leads' in [c['key'] for c in f['checks']]


def test_a_deal_with_no_close_date_costs_the_close_date_weight(clean):
    with flask_app.app_context():
        db.session.add(Opportunity(
            opp_number=TAG + 'OPP1', stage='Proposal',
            owner_emp_code=REP, expected_close_date=None,
            created_at=datetime.utcnow(), updated_at=datetime.utcnow()))
        db.session.commit()
    _assert_full_deduction('close_date', _score())


def test_a_quoted_lead_with_no_quote_costs_the_rfq_quotation_weight(clean):
    with flask_app.app_context():
        for row in Lead.query.filter(Lead.company.like(TAG + '%')).all():
            row.stage = 'Quoted'
            row.quoted_amount_inr = None
            row.quote_date = None
        db.session.commit()
    f = _assert_full_deduction('rfq_quotation', _score())
    assert 'quoted_without_quote' in [c['key'] for c in f['checks']]


def test_a_won_lead_with_no_value_costs_the_won_handover_weight(clean):
    with flask_app.app_context():
        for row in Lead.query.filter(Lead.company.like(TAG + '%')).all():
            row.stage = 'Won'
            row.estimated_value_inr = None
            row.quoted_amount_inr = None
            row.opportunity_value_num = None
            row.quote_value_num = None
        db.session.commit()
    f = _assert_full_deduction('won_handover', _score())
    assert 'won_leads_no_value' in [c['key'] for c in f['checks']]


def test_nothing_to_get_wrong_deducts_nothing(clean):
    """A factor with an empty population is neither credited nor
    punished: a rep with no lost deals is not a rep with bad hygiene."""
    f = _factor(_score(), 'lost_reason')
    assert f['population'] == 0 and f['count'] == 0 and f['lost'] == 0


def test_two_checks_cannot_cost_more_than_the_factor_is_worth(clean):
    with flask_app.app_context():
        for row in Lead.query.filter(Lead.company.like(TAG + '%')).all():
            row.stage = 'Won'
            row.estimated_value_inr = None
            row.quoted_amount_inr = None
            row.opportunity_value_num = None
            row.quote_value_num = None
        db.session.add(Opportunity(
            opp_number=TAG + 'OPP2', stage='Won', owner_emp_code=REP,
            won_at=datetime.utcnow(), value_inr=None,
            created_at=datetime.utcnow(), updated_at=datetime.utcnow()))
        db.session.commit()
    f = _factor(_score(), 'won_handover')
    assert f['share'] <= 1.0
    assert f['lost'] <= hygiene.BY_KEY['won_handover']['weight']
    assert _score()['score'] >= 0


# ── the records behind a deduction ───────────────────────────────────
def test_a_factor_page_lists_exactly_the_records_that_caused_it(clean):
    with flask_app.app_context():
        for row in Lead.query.filter(Lead.company.like(TAG + '%')).all():
            row.followup_date = None
        db.session.commit()
        detail = hygiene.factor_records(sc_mod.for_employee(REP),
                                        'next_action')
    assert detail['count'] >= 1
    names = [r['name'] for g in detail['groups'] for r in g['records']]
    assert any(TAG in n for n in names)
    assert all(g['label'] for g in detail['groups'])
    assert detail['lost'] == hygiene.BY_KEY['next_action']['weight']


def test_an_unknown_factor_is_not_invented(clean):
    with flask_app.app_context():
        assert hygiene.factor_records(sc_mod.for_employee(REP), 'nope') is None


# ── scope ────────────────────────────────────────────────────────────
def test_one_persons_mess_does_not_lower_another_persons_score(clean):
    with flask_app.app_context():
        bad = _lead('Neglected Ltd', OTHER, followup_date=None)
        db.session.commit()
        bad_id = bad.id
    assert _score(REP)['score'] == 100
    other = _score(OTHER)
    assert other['score'] < 100
    f = _factor(other, 'next_action')
    assert bad_id in [r['id'] for r in f['records']]


def test_a_vertical_head_sees_the_team_and_a_rep_cannot_ask_about_others(clean):
    with flask_app.app_context():
        _lead('Neglected Ltd', OTHER, followup_date=None)
        db.session.commit()
        head = hygiene.score(sc_mod.for_employee(HEAD))
        assert _factor(head, 'next_action')['count'] >= 1
        with pytest.raises(hygiene.OutsideScope):
            hygiene.score(sc_mod.for_employee(REP), emp_code=OTHER)


def test_narrowing_can_never_widen(clean):
    with flask_app.app_context():
        sc = sc_mod.for_employee(HEAD)
        narrowed = hygiene.narrow(sc, emp_code=OTHER)
        assert narrowed.codes == {OTHER}
        assert hygiene.levels(sc_mod.for_employee(REP)) == ['user']
        assert 'vertical' in hygiene.levels(sc_mod.for_employee(HEAD))
        assert 'company' in hygiene.levels(hygiene.dq.system_scope())


def test_the_three_levels_are_the_same_sum_over_a_narrower_scope(clean):
    with flask_app.app_context():
        _lead('Neglected Ltd', OTHER, followup_date=None)
        db.session.commit()
        sc = sc_mod.for_employee(HEAD)
        person = hygiene.score(sc, emp_code=OTHER)
        vertical = hygiene.score(sc, vertical='Hygiene Vertical')
        assert person['level'] == 'user'
        assert vertical['level'] == 'vertical'
        assert _factor(person, 'next_action')['count'] <= \
            _factor(vertical, 'next_action')['count']


# ── the pages and the API ────────────────────────────────────────────
def test_the_api_needs_a_session(clean):
    assert flask_app.test_client().get('/api/hygiene').status_code == 401


def test_the_api_answers_with_the_breakdown(clean):
    d = _client(REP).get('/api/hygiene').get_json()
    assert d['ok'] and d['score'] == 100
    assert {f['key'] for f in d['factors']} == set(hygiene.BY_KEY)
    assert d['levels'] == ['user']


def test_the_api_refuses_someone_outside_your_access(clean):
    r = _client(REP).get(f'/api/hygiene?for={OTHER}')
    assert r.status_code == 403
    assert _client(HEAD).get(f'/api/hygiene?for={OTHER}').status_code == 200


def test_the_pages_render(clean):
    c = _client(REP)
    assert c.get('/hygiene').status_code == 200
    page = c.get('/hygiene/factor/next_action')
    assert page.status_code == 200
    assert b'A next action is set' in page.data
    assert c.get('/hygiene/factor/nope').status_code == 404
    assert flask_app.test_client().get('/hygiene').status_code in (301, 302)


def test_every_route_is_gated_by_the_name_the_scanner_knows():
    """scripts/generate_reference_docs.py reads decorator names, so the
    gate has to be called @_signed_in to be recorded as one."""
    src = open(os.path.join(_ROOT, 'app', 'hygiene', 'routes.py')).read()
    assert src.count('@_signed_in') == src.count('@bp.route')


def test_the_page_sends_a_csrf_token_with_every_write():
    html = open(os.path.join(_ROOT, 'templates', 'hygiene',
                             'home.html')).read()
    assert "'X-CSRFToken': csrfToken()" in html
    assert 'csrf_token=' in html
    body = html[html.index('<script>'):]
    at = body.index('/api/hygiene/nudge')
    assert 'postJson(' in body[max(0, at - 300):at + 100]


def test_a_write_without_the_token_is_refused(clean):
    was = flask_app.config.get('WTF_CSRF_ENABLED')
    flask_app.config['WTF_CSRF_ENABLED'] = True
    try:
        r = _client(HEAD).post('/api/hygiene/nudge',
                               data=json.dumps({'to': REP, 'message': 'x'}),
                               content_type='application/json')
        assert r.status_code in (400, 403)
    finally:
        flask_app.config['WTF_CSRF_ENABLED'] = was


def test_a_nudge_is_logged_and_cannot_leave_your_scope(clean):
    r = _client(REP).post('/api/hygiene/nudge',
                          data=json.dumps({'to': OTHER, 'message': 'hi'}),
                          content_type='application/json')
    assert r.status_code == 403
    r = _client(HEAD).post(
        '/api/hygiene/nudge',
        data=json.dumps({'to': REP, 'message': 'Please set follow-ups',
                         'factor': 'next_action'}),
        content_type='application/json')
    assert r.status_code == 200 and r.get_json()['notified']
    with flask_app.app_context():
        from app.models.audit import AuditEvent
        from app.models.notification import Notification
        n = (Notification.query.filter_by(user_id=REP)
             .order_by(Notification.id.desc()).first())
        assert n and n.action_url == '/hygiene/factor/next_action'
        assert AuditEvent.query.filter_by(action='hygiene.nudge').first()
