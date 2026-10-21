"""Quote margin rules decided on 21 Oct 2026 (G9, H1, H3, H4, H7, H8).

* Freight Forwarding / Project Freight buy rates and margin: only the
  named viewers — not even an unrestricted admin outside the list.
* Other verticals: preparer, lead owner, head of the vertical,
  unrestricted viewers. Nobody else.
* The FF margin endpoints refuse a caller outside the quote's scope
  (404) and a caller who may not see buy (403).
* With FEATURE_MARGIN_GATE on, a head approves only at or above every
  line's minimum and up to the limit; otherwise the director.
"""
import os
import sys
import tempfile
from decimal import Decimal

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'MarginTest12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'margin.db')

from app import app as flask_app, db, Employee, Lead, Company  # noqa: E402
from app.access.service import set_profile                     # noqa: E402
from app.models.access import DataScope                        # noqa: E402
from app.models.email_admin import VerticalAlias, VerticalHead  # noqa: E402
from app.models.quote import Quote, QuoteLine                  # noqa: E402
from app.services import admin_settings, flags, margin_policy  # noqa: E402
from app.services import vertical_heads                        # noqa: E402

flask_app.config['WTF_CSRF_ENABLED'] = False

DIRECTOR, VIEWER, ADMIN = 'MDIR', 'MVIEW', 'MADM'
HT_HEAD, HT_REP, OTHER = 'MHTH', 'MHTR', 'MOTH'


_CREATED = []


def _upsert(model, keys, **values):
    """Get-or-create, so the fixture works on the shared suite database.
    Rows this module creates are removed at the end of it."""
    row = model.query.filter_by(**keys).first()
    if row is None:
        row = model(**keys)
        db.session.add(row)
        _CREATED.append(row)
    for k, v in values.items():
        setattr(row, k, v)
    return row


def _cleanup(extra=()):
    for row in list(extra) + list(reversed(_CREATED)):
        try:
            db.session.delete(row)
        except Exception:
            pass
    db.session.commit()
    _CREATED.clear()


_SEQ = [0]


def _quote(lead, prepared_by, lines, currency='INR'):
    _SEQ[0] += 1
    q = Quote(quote_number=f'QUO-T-{_SEQ[0]:04d}',
              lead_id=lead.id, account_id=lead.company_id, status='Draft',
              prepared_by_id=prepared_by, created_by_id=prepared_by,
              currency=currency)
    db.session.add(q)
    db.session.flush()
    for i, (sell, cost, source) in enumerate(lines, 1):
        ln = QuoteLine(quote_id=q.id, line_no=i, service='Move',
                       quantity=Decimal('1'), unit_rate=Decimal(str(sell)),
                       cost_inr=(Decimal(str(cost)) if cost is not None
                                 else None),
                       vehicle_source=source)
        ln.recompute_total()
        db.session.add(ln)
    db.session.flush()
    q.recompute_totals()
    return q.id


@pytest.fixture(scope='module')
def world():
    with flask_app.app_context():
        db.create_all()
        for code, vertical, head in (
                (DIRECTOR, 'All', False), (VIEWER, 'Project Freight', False),
                (ADMIN, 'All', False), (HT_HEAD, 'Heavy Transport', True),
                (HT_REP, 'Heavy Transport', False),
                (OTHER, 'Warehousing', False)):
            _upsert(Employee, {'emp_code': code}, name=code, role='user',
                    vertical=vertical, is_active=True,
                    is_vertical_head=head, must_change_pw=False,
                    is_super_admin=False)
        _upsert(VerticalHead, {'vertical': 'Heavy Transport'},
                head_emp_code=HT_HEAD, active=True)
        _upsert(VerticalHead, {'vertical': 'Project Freight'}, active=True)
        _upsert(VerticalAlias, {'alias': 'Freight Forwarding'},
                vertical='Project Freight')
        acct = Company(name='Margin Test Ltd', is_active=True,
                       country='India', pic_emp_code=HT_REP)
        abroad = Company(name='Margin Overseas GmbH', is_active=True,
                         country='Germany', pic_emp_code=HT_REP)
        db.session.add_all([acct, abroad])
        db.session.flush()
        ht = Lead(company='Margin HT', source='manual', stage='Quoted',
                  assigned_to=HT_REP, procam_vertical='Heavy Transport',
                  company_id=acct.id)
        ff = Lead(company='Margin FF', source='manual', stage='Quoted',
                  assigned_to=HT_REP, procam_vertical='Freight Forwarding',
                  company_id=acct.id)
        ov = Lead(company='Margin Overseas', source='manual', stage='Quoted',
                  assigned_to=HT_REP, procam_vertical='Warehousing',
                  company_id=abroad.id)
        db.session.add_all([ht, ff, ov])
        db.session.flush()
        ids = {
            'acct': acct.id, 'abroad': abroad.id,
            'ht_lead': ht.id, 'ff_lead': ff.id, 'ov_lead': ov.id,
            # PTM own at 40% — above 38
            'ht_ok': _quote(ht, HT_REP, [(100000, 60000, 'PTM-Own')]),
            # PTM own at 30% — below 38
            'ht_low': _quote(ht, HT_REP, [(100000, 70000, 'PTM-Own')]),
            # PTM market at 30% — fine for market (14)
            'ht_market': _quote(ht, HT_REP, [(100000, 70000, 'PTM-Market')]),
            # 40% but ₹30 lakh — over the head limit
            'ht_big': _quote(ht, HT_REP, [(3000000, 1800000, 'PTM-Own')]),
            'ff': _quote(ff, HT_REP, [(1000, 950, None)]),
        }
        db.session.commit()
        admin_settings.save('margin_viewers_ff', [DIRECTOR, VIEWER],
                            actor='test')
        admin_settings.save('director_approvers', [DIRECTOR], actor='test')
        set_profile(ADMIN, DataScope.ALL, ['module.quotes'], actor='test')
        set_profile(DIRECTOR, DataScope.ALL, ['module.quotes'], actor='test')
        set_profile(HT_HEAD, DataScope.VERTICAL, ['module.quotes'], actor='test')
        set_profile(HT_REP, DataScope.OWN, ['module.quotes'], actor='test')
        set_profile(OTHER, DataScope.OWN, ['module.quotes'], actor='test')
        set_profile(VIEWER, DataScope.OWN, ['module.quotes'], actor='test')
        vertical_heads.cache_clear()
    yield ids
    with flask_app.app_context():
        from app.models.access import AccessProfile
        from app.models.integration import AppSetting
        lead_ids = [ids['ht_lead'], ids['ff_lead'], ids['ov_lead']]
        qids = [q.id for q in Quote.query.filter(
            Quote.lead_id.in_(lead_ids)).all()]
        QuoteLine.query.filter(QuoteLine.quote_id.in_(qids)).delete(
            synchronize_session=False)
        Quote.query.filter(Quote.id.in_(qids)).delete(
            synchronize_session=False)
        Lead.query.filter(Lead.id.in_(lead_ids)).delete(
            synchronize_session=False)
        Company.query.filter(Company.id.in_(
            [ids['acct'], ids['abroad']])).delete(synchronize_session=False)
        AccessProfile.query.filter(AccessProfile.emp_code.in_(
            [DIRECTOR, VIEWER, ADMIN, HT_HEAD, HT_REP, OTHER])).delete(
            synchronize_session=False)
        AppSetting.query.filter(AppSetting.key.in_(
            ['margin_viewers_ff', 'director_approvers'])).delete(
            synchronize_session=False)
        db.session.commit()
        _cleanup()
        vertical_heads.cache_clear()


def _client(code):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role='user', sv=0)
    return c


def _q(qid):
    return db.session.get(Quote, qid)


# ── visibility ───────────────────────────────────────────────────────
def test_ff_buy_rates_only_for_named_viewers(world):
    with flask_app.app_context():
        q = _q(world['ff'])
        assert margin_policy.can_see_buy(q, DIRECTOR)
        assert margin_policy.can_see_buy(q, VIEWER)
        # unrestricted admin, not on the list
        assert not margin_policy.can_see_buy(q, ADMIN)
        # the lead owner who prepared it, not on the list
        assert not margin_policy.can_see_buy(q, HT_REP)


def test_other_vertical_owner_head_and_admin_see_buy(world):
    with flask_app.app_context():
        q = _q(world['ht_ok'])
        assert margin_policy.can_see_buy(q, HT_REP)
        assert margin_policy.can_see_buy(q, HT_HEAD)
        assert margin_policy.can_see_buy(q, ADMIN)
        assert not margin_policy.can_see_buy(q, OTHER)


def test_quote_api_strips_cost_for_unlisted_admin(world):
    r = _client(ADMIN).get(f'/api/quotes/{world["ff"]}')
    assert r.status_code == 200
    body = r.get_json()['quote']
    assert body['cost_hidden'] is True
    line = body['lines'][0]
    assert line['cost_inr'] is None and line['margin_pct'] is None
    # the sell side is still there — they can still read the quote
    assert line['unit_rate'] == 1000


def test_quote_api_keeps_cost_for_viewer(world):
    with flask_app.app_context():
        set_profile(VIEWER, DataScope.ALL, ['module.quotes'], actor='test')
    try:
        body = _client(VIEWER).get(f'/api/quotes/{world["ff"]}').get_json()
        assert body['quote']['cost_hidden'] is False
        assert body['quote']['lines'][0]['cost_inr'] == 950
    finally:
        with flask_app.app_context():
            set_profile(VIEWER, DataScope.OWN, ['module.quotes'], actor='test')


def test_ff_margin_endpoint_refuses_unlisted_and_out_of_scope(
        world, monkeypatch):
    monkeypatch.setattr(flags, 'on', lambda name, default=None:
                        name == 'FEATURE_FF_CRM')
    path = f'/api/ff/quotes/{world["ff"]}/margin'
    # in scope (unrestricted) but not a named viewer
    assert _client(ADMIN).get(path).status_code == 403
    # a named viewer outside the quote's scope: 404, as by-id routes do
    assert _client(VIEWER).get(path).status_code == 404
    # the director: in scope and named
    assert _client(DIRECTOR).get(path).status_code == 200


# ── minimum margins ──────────────────────────────────────────────────
def test_floor_by_vehicle_source(world):
    with flask_app.app_context():
        assert margin_policy.check(_q(world['ht_ok']))['below_floor'] == []
        low = margin_policy.check(_q(world['ht_low']))['below_floor']
        assert low and low[0]['floor_pct'] == 38
        assert margin_policy.check(_q(world['ht_market']))['below_floor'] \
            == []


def test_value_over_limit_needs_director(world):
    with flask_app.app_context():
        result = margin_policy.check(_q(world['ht_big']))
        assert result['over_limit'] and result['needs_director']


def test_line_without_cost_is_flagged_not_passed(world):
    with flask_app.app_context():
        lead = db.session.get(Lead, world['ht_lead'])
        qid = _quote(lead, HT_REP, [(5000, None, 'Mechanical')])
        db.session.commit()
        result = margin_policy.check(_q(qid))
        assert result['no_cost'] and not result['below_floor']


# ── approval, with the gate on ───────────────────────────────────────
def _approve(code, qid):
    with flask_app.app_context():
        q = _q(qid)
        q.status = 'Awaiting Approval'
        db.session.commit()
    return _client(code).post(f'/api/quotes/{qid}/approve')


@pytest.fixture()
def gate(monkeypatch):
    monkeypatch.setattr(flags, 'on', lambda name, default=None:
                        name == 'FEATURE_MARGIN_GATE')


def test_head_approves_within_floor_and_limit(world, gate):
    assert _approve(HT_HEAD, world['ht_market']).status_code == 200


def test_head_refused_below_floor_director_allowed(world, gate):
    r = _approve(HT_HEAD, world['ht_low'])
    assert r.status_code == 403
    assert 'director' in r.get_json()['error']
    assert _approve(DIRECTOR, world['ht_low']).status_code == 200


def test_head_refused_over_limit(world, gate):
    assert _approve(HT_HEAD, world['ht_big']).status_code == 403


def test_unrestricted_admin_is_not_the_director(world, gate):
    """Whole-company scope is not approval authority under the gate."""
    assert _approve(ADMIN, world['ht_ok']).status_code == 403


def test_rep_cannot_approve(world, gate):
    assert _approve(OTHER, world['ht_ok']).status_code in (403, 404)


# ── defaults at creation ─────────────────────────────────────────────
def test_validity_default_by_vertical(world):
    r = _client(ADMIN).post('/api/quotes', json={'lead_id': world['ht_lead'],
                                                 'account_id': world['acct']})
    q = r.get_json()['quote']
    from datetime import date
    assert (date.fromisoformat(q['validity_until'])
            - date.fromisoformat(q['quote_date'])).days == 15
    assert q['currency'] == 'INR'


def test_overseas_client_quoted_in_usd(world):
    r = _client(ADMIN).post('/api/quotes', json={'lead_id': world['ov_lead'],
                                                 'account_id': world['abroad']})
    q = r.get_json()['quote']
    assert q['currency'] == 'USD'
    from datetime import date
    assert (date.fromisoformat(q['validity_until'])
            - date.fromisoformat(q['quote_date'])).days == 30


def test_typed_values_win(world):
    r = _client(ADMIN).post('/api/quotes', json={
        'lead_id': world['ov_lead'], 'account_id': world['abroad'],
        'currency': 'EUR', 'validity_until': '2030-01-01'})
    q = r.get_json()['quote']
    assert q['currency'] == 'EUR' and q['validity_until'] == '2030-01-01'


# ── the CRM rules screen ─────────────────────────────────────────────
def test_rules_screen_saves_typed_values_and_refuses_bad_ones(world):
    with flask_app.app_context():
        set_profile(ADMIN, DataScope.ALL, ['module.quotes', 'admin.email'],
                    actor='test')
    c = _client(ADMIN)
    assert c.get('/admin/crm-rules').status_code == 200
    r = c.post('/admin/crm-rules', data={
        'director_approvers': 'mdir, mview',
        'margin_floors': 'PTM_OWN = 40\nWAREHOUSING = 18.5',
        'head_approval_limit_inr': '30,00,000'})
    assert r.status_code == 200 and b'saved' in r.data
    with flask_app.app_context():
        assert admin_settings.get('director_approvers') == [DIRECTOR, VIEWER]
        assert admin_settings.get('margin_floors') == {
            'PTM_OWN': 40, 'WAREHOUSING': 18.5}
        assert admin_settings.get('head_approval_limit_inr') == 3000000
    r = c.post('/admin/crm-rules', data={'margin_floors': 'PTM_OWN forty'})
    assert b'nothing was saved' in r.data
    with flask_app.app_context():
        assert admin_settings.get('margin_floors')['PTM_OWN'] == 40
        from app.models.integration import AppSetting
        AppSetting.query.filter(AppSetting.key.in_(
            ['margin_floors', 'head_approval_limit_inr'])).delete(
            synchronize_session=False)
        db.session.commit()
        admin_settings.save('director_approvers', [DIRECTOR], actor='test')


def test_rules_screen_needs_admin_email(world):
    assert _client(OTHER).get('/admin/crm-rules').status_code in (302, 403)
