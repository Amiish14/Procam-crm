"""The block register, asked by another server.

The browser endpoint needs a session, so the TMS could not use it
without borrowing a person's cookie. This is the same decision behind
the same bearer token — and, importantly, it only ever reads.
"""
import os
import sys
import tempfile
from datetime import datetime

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'restriction-api-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'RestrictApi12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'restrictapi.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Lead = _main.Company, _main.Lead

from app.models.integration import CrmTmsLink, IntegrationLog  # noqa: E402
from app.models.restriction import (                      # noqa: E402
    BLOCKED, CAUTION, ClientRestriction, ClientRestrictionEvent)
from app.services import client_restrictions as restrictions   # noqa: E402
from app.services import integration as integ            # noqa: E402

TOKEN = 'restriction-api-token-not-real'
TAG = 'RAP-'
ENDPOINT = integ.API_PREFIX + '/client-restriction/check'


def _wipe():
    ids = [r.id for r in ClientRestriction.query.all()]
    if ids:
        ClientRestrictionEvent.query.filter(
            ClientRestrictionEvent.restriction_id.in_(ids)).delete(
                synchronize_session=False)
        ClientRestriction.query.delete(synchronize_session=False)
    CrmTmsLink.query.delete(synchronize_session=False)
    IntegrationLog.query.delete(synchronize_session=False)
    Lead.query.filter(Lead.company.like(TAG + '%')).delete(
        synchronize_session=False)
    Company.query.filter(Company.name.like(TAG + '%')).delete(
        synchronize_session=False)
    db.session.commit()
    restrictions.cache_clear()


@pytest.fixture()
def world(monkeypatch):
    monkeypatch.setenv('CRM_INTEGRATION_TOKENS', f'procam-tms:{TOKEN}')
    with flask_app.app_context():
        db.create_all()
        _wipe()
        yield
        _wipe()


@pytest.fixture()
def tms(world):
    client = flask_app.test_client()

    def _ask(payload, token=TOKEN):
        headers = {'Authorization': f'Bearer {token}'} if token else {}
        return client.post(ENDPOINT, json=payload, headers=headers)
    return _ask


def _restrict(name, *, status=BLOCKED, account_id=None, gstin=None,
              review=None):
    row = ClientRestriction(
        status=status, company_name=name,
        name_key=restrictions.normalise(name),
        reason_category='Payment default / outstanding',
        reason_detail='Outstanding against invoices since August.',
        recommended_by='SYS', approved_by='DIR1',
        recommended_at=datetime(2026, 9, 1, 10, 0),
        approved_at=datetime(2026, 9, 2, 11, 30),
        linked_account_id=account_id, gstin=gstin, review_date=review)
    row.aliases = []
    row.domains = []
    row.emails = []
    row.business_units = []
    db.session.add(row)
    db.session.commit()
    restrictions.cache_clear()
    return row


# ── allowed ──────────────────────────────────────────────────────────
def test_an_unrestricted_client_is_allowed(tms):
    body = tms({'company_name': TAG + 'Ordinary Customer'}).get_json()
    assert body['ok'] is True
    assert body['blocked'] is False
    assert body['level'] == 'none'
    assert body['restriction_id'] is None
    assert body['request_id']


# ── blocked ──────────────────────────────────────────────────────────
def test_a_blocked_client_is_reported_with_its_reason(tms):
    row = _restrict(TAG + 'Blocked Customer Ltd')
    body = tms({'company_name': TAG + 'Blocked Customer Limited'}).get_json()
    assert body['blocked'] is True
    assert body['level'] == 'blocked'
    assert body['restriction_id'] == row.id
    assert body['reason_code'] == 'Payment default / outstanding'
    assert 'Outstanding against invoices' in body['reason']
    assert body['effective_from'].startswith('2026-09-02')


def test_no_expiry_is_invented(tms):
    """The register has no expiry column. A block runs until somebody
    lifts it, and review_date is a reminder rather than an end."""
    _restrict(TAG + 'Blocked Customer Ltd', review=datetime(2027, 3, 1).date())
    body = tms({'company_name': TAG + 'Blocked Customer Ltd'}).get_json()
    assert body['effective_until'] is None
    assert body['review_date'] == '2027-03-01'


def test_a_caution_is_reported_as_not_blocked(tms):
    _restrict(TAG + 'Watchlist Co', status=CAUTION)
    body = tms({'company_name': TAG + 'Watchlist Co'}).get_json()
    assert body['blocked'] is False
    assert body['level'] == 'caution'
    assert body['restriction_id'] is not None


# ── identifiers, most stable first ───────────────────────────────────
def test_the_crm_account_id_is_matched(tms):
    account = Company(name=TAG + 'By Id Customer', is_active=True)
    db.session.add(account)
    db.session.commit()
    _restrict(TAG + 'Something Else Entirely', account_id=account.id)
    body = tms({'crm_account_id': account.id}).get_json()
    assert body['blocked'] is True
    assert body['matched_on'] == 'account'


def test_a_gstin_is_matched(tms):
    _restrict(TAG + 'GST Customer', gstin='27AAAAA0000A1Z5')
    body = tms({'gstin': '27aaaaa0000a1z5'}).get_json()
    assert body['blocked'] is True and body['matched_on'] == 'gstin'


def test_a_tms_project_resolves_through_the_link_table(tms):
    account = Company(name=TAG + 'Linked Customer', is_active=True)
    db.session.add(account)
    db.session.commit()
    lead = Lead(company=TAG + 'Linked Customer', stage='New',
                company_id=account.id)
    db.session.add(lead)
    db.session.commit()
    db.session.add(CrmTmsLink(crm_lead_id=lead.id,
                              crm_account_id=account.id,
                              tms_project_id='TMS-PRJ-7001',
                              link_type='project'))
    db.session.commit()
    _restrict(TAG + 'Linked Customer', account_id=account.id)

    body = tms({'tms_project_id': 'TMS-PRJ-7001'}).get_json()
    assert body['blocked'] is True
    assert body['resolved_crm_account_id'] == account.id
    assert body['resolved_crm_lead_id'] == lead.id


def test_a_near_miss_on_the_name_is_a_caution_not_a_block(tms):
    _restrict('Thermax Engineering Limited')
    body = tms({'company_name': 'Thermax Engineers Pvt Ltd'}).get_json()
    assert body['blocked'] is False
    assert body['level'] == 'caution'
    assert body['matched_on'] == 'name-possible'


def test_a_business_unit_narrows_the_answer(tms):
    row = _restrict(TAG + 'One Unit Customer')
    row.business_units = ['PLPL']
    db.session.commit()
    restrictions.cache_clear()
    assert tms({'company_name': TAG + 'One Unit Customer',
                'business_unit': 'PLPL'}).get_json()['blocked'] is True
    assert tms({'company_name': TAG + 'One Unit Customer',
                'business_unit': 'PWLPL'}).get_json()['blocked'] is False


# ── unknown ──────────────────────────────────────────────────────────
def test_an_unknown_account_id_is_allowed_not_an_error(tms):
    resp = tms({'crm_account_id': 99999123})
    assert resp.status_code == 200
    assert resp.get_json()['blocked'] is False


def test_an_unknown_tms_project_is_allowed_not_an_error(tms):
    """The TMS calls this before every PO, Job and LR, and most of its
    projects are not linked to a CRM lead. A 400 for the ordinary case
    teaches the caller to ignore the endpoint."""
    resp = tms({'tms_project_id': 'TMS-PRJ-NOPE'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['blocked'] is False
    assert body['level'] == 'none'
    assert body['resolved_crm_account_id'] is None
    assert 'nothing to check' in body['note']


def test_an_unknown_project_sent_with_a_name_still_checks_the_name(tms):
    _restrict(TAG + 'Named Anyway Ltd')
    body = tms({'tms_project_id': 'TMS-PRJ-NOPE',
                'company_name': TAG + 'Named Anyway Ltd'}).get_json()
    assert body['blocked'] is True


# ── authentication ───────────────────────────────────────────────────
def test_no_token_is_refused(tms):
    resp = tms({'company_name': 'anyone'}, token=None)
    assert resp.status_code == 401
    assert resp.get_json()['error']['code'] == 'unauthorized'


def test_a_wrong_token_is_refused(tms):
    resp = tms({'company_name': 'anyone'}, token='not-the-token')
    assert resp.status_code == 401


def test_a_browser_session_is_not_accepted_here(world):
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess['emp_code'] = 'SOMEONE'
        sess['role'] = 'admin'
    assert client.post(ENDPOINT, json={'company_name': 'x'}
                       ).status_code == 401


def test_the_browser_endpoint_still_needs_a_session(world):
    """The old route is untouched. Weakening it so one more caller
    could reach it would have been the wrong repair."""
    resp = flask_app.test_client().get(
        '/api/client-restriction/check?company_name=anything')
    assert resp.status_code == 401


# ── malformed ────────────────────────────────────────────────────────
def test_an_empty_body_is_refused_clearly(tms):
    resp = tms({})
    assert resp.status_code == 400
    assert resp.get_json()['error']['code'] == 'missing_identifier'


def test_a_non_numeric_account_id_is_refused(tms):
    resp = tms({'crm_account_id': 'not-a-number'})
    assert resp.status_code == 400
    assert resp.get_json()['error']['code'] == 'malformed_request'


def test_a_json_array_is_refused(tms):
    resp = flask_app.test_client().post(
        ENDPOINT, json=['not', 'an', 'object'],
        headers={'Authorization': f'Bearer {TOKEN}'})
    assert resp.status_code == 400
    assert resp.get_json()['error']['code'] == 'malformed_request'


def test_every_error_has_the_same_shape(tms):
    for payload, token in (({}, TOKEN), ({'company_name': 'x'}, 'wrong')):
        body = tms(payload, token=token).get_json()
        assert body['ok'] is False
        assert set(body['error']) == {'code', 'message'}
        assert 'request_id' in body


# ── read-only ────────────────────────────────────────────────────────
def test_the_endpoint_never_changes_a_restriction(tms):
    row = _restrict(TAG + 'Untouched Customer')
    before = (row.status, row.reason_detail, row.approved_by,
              row.lifted_at, row.review_date)
    for _ in range(3):
        tms({'company_name': TAG + 'Untouched Customer'})
    db.session.expire_all()
    again = ClientRestriction.query.get(row.id)
    assert (again.status, again.reason_detail, again.approved_by,
            again.lifted_at, again.review_date) == before


def test_it_never_creates_a_restriction(tms):
    before = ClientRestriction.query.count()
    tms({'company_name': TAG + 'Brand New Name'})
    tms({'crm_account_id': 4242})
    assert ClientRestriction.query.count() == before


def test_asking_repeatedly_is_safe(tms):
    _restrict(TAG + 'Repeat Customer')
    answers = [tms({'company_name': TAG + 'Repeat Customer'}).get_json()
               for _ in range(5)]
    assert all(a['blocked'] for a in answers)
    assert len({a['restriction_id'] for a in answers}) == 1


# ── logging ──────────────────────────────────────────────────────────
def test_the_call_is_logged_with_the_caller(tms):
    _restrict(TAG + 'Logged Customer')
    tms({'company_name': TAG + 'Logged Customer'})
    row = (IntegrationLog.query.filter_by(direction='inbound', status='ok')
           .order_by(IntegrationLog.id.desc()).first())
    assert row is not None
    assert row.caller == 'procam-tms'
    assert row.endpoint.endswith('/client-restriction/check')
    assert row.request_id


def test_the_attempt_is_recorded_against_the_register(tms):
    """"Who keeps trying to raise work for a blocked client" should
    not depend on which system they tried it from."""
    row = _restrict(TAG + 'Attempted Customer')
    tms({'company_name': TAG + 'Attempted Customer'})
    attempts = ClientRestrictionEvent.query.filter_by(
        restriction_id=row.id, action='attempt_blocked').all()
    assert len(attempts) == 1
    assert attempts[0].user_id == 'tms:procam-tms'


def test_a_clear_answer_records_no_attempt(tms):
    tms({'company_name': TAG + 'Perfectly Fine Customer'})
    assert ClientRestrictionEvent.query.count() == 0


def test_the_token_never_reaches_the_log(tms):
    tms({'company_name': TAG + 'Logged Customer'})
    blob = ' '.join((l.request_summary or '') + (l.response_summary or '')
                    for l in IntegrationLog.query.all())
    assert TOKEN not in blob
