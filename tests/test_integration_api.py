"""The CRM ↔ TMS seam.

Two applications, two databases, no shared code. What is tested here
is the contract the TMS will be written against: who may call, what a
retry does, and what gets written down.
"""
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'integration-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Integration12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'integration.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Employee, Lead = _main.Company, _main.Employee, _main.Lead

from app.models.integration import (                      # noqa: E402
    CrmTmsLink, IntegrationLog)
from app.services import integration as integ            # noqa: E402

TOKEN = 'a-long-token-that-is-not-a-real-one'
TAG = 'INT-'


def _wipe():
    IntegrationLog.query.delete(synchronize_session=False)
    CrmTmsLink.query.delete(synchronize_session=False)
    Lead.query.filter(Lead.company.like(TAG + '%')).delete(
        synchronize_session=False)
    Company.query.filter(Company.name.like(TAG + '%')).delete(
        synchronize_session=False)
    db.session.commit()


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

    def _call(method, path, **kw):
        headers = kw.pop('headers', {})
        headers.setdefault('Authorization', f'Bearer {TOKEN}')
        return getattr(client, method)(integ.API_PREFIX + path,
                                       headers=headers, **kw)
    _call.raw = client
    return _call


# ── who may call ─────────────────────────────────────────────────────
def test_a_valid_token_is_accepted(tms):
    body = tms('get', '/ping').get_json()
    assert body['ok'] and body['caller'] == 'procam-tms'
    assert body['request_id']


def test_no_token_is_refused(tms):
    resp = tms('get', '/ping', headers={'Authorization': ''})
    assert resp.status_code == 401
    assert resp.get_json()['error']['code'] == 'unauthorized'


def test_a_wrong_token_is_refused(tms):
    resp = tms('get', '/ping', headers={'Authorization': 'Bearer nope'})
    assert resp.status_code == 401


def test_a_browser_session_is_not_a_system_credential(world):
    """These routes have no session fallback on purpose: a person's
    cookie must not be usable as a server-to-server credential."""
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess['emp_code'] = 'ANYONE'
        sess['role'] = 'admin'
    assert client.get(integ.API_PREFIX + '/ping').status_code == 401


def test_nothing_is_reachable_when_no_token_is_configured(world, monkeypatch):
    monkeypatch.delenv('CRM_INTEGRATION_TOKENS', raising=False)
    resp = flask_app.test_client().get(integ.API_PREFIX + '/ping')
    assert resp.status_code == 503
    assert resp.get_json()['error']['code'] == 'integration_disabled'


def test_every_refusal_has_the_same_shape(tms):
    body = tms('get', '/ping', headers={'Authorization': 'Bearer x'}).get_json()
    assert body['ok'] is False
    assert set(body['error']) == {'code', 'message'}
    assert 'request_id' in body


# ── accounts ─────────────────────────────────────────────────────────
def test_an_account_is_found_by_name_however_it_is_spelled(tms):
    db.session.add(Company(name=TAG + 'Steelworks Engineering Limited',
                           is_active=True))
    db.session.commit()
    body = tms('get', '/accounts?name=' + TAG + 'Steelworks Engineering Ltd'
               ).get_json()
    assert body['found'] and 'Steelworks' in body['account']['name']


def test_creating_an_account_that_exists_returns_it_rather_than_a_copy(tms):
    db.session.add(Company(name=TAG + 'Known Customer Ltd', is_active=True))
    db.session.commit()
    body = tms('post', '/accounts',
               json={'name': TAG + 'Known Customer Limited'}).get_json()
    assert body['created'] is False
    assert Company.query.filter(
        Company.name.like(TAG + 'Known Customer%')).count() == 1


def test_a_new_account_is_created_and_marked_as_the_tms_caller(tms):
    resp = tms('post', '/accounts', json={'name': TAG + 'Brand New Co',
                                          'city': 'Pune'})
    assert resp.status_code == 201
    row = Company.query.filter_by(name=TAG + 'Brand New Co').one()
    assert row.created_by == 'tms:procam-tms'


# ── the block register reaches across ────────────────────────────────
def test_a_blocked_client_cannot_be_created_from_the_tms(tms):
    """Management stopping business with a client is not undone by the
    work arriving in another system."""
    from datetime import datetime

    from app.models.restriction import BLOCKED, ClientRestriction
    from app.services import client_restrictions as restrictions

    row = ClientRestriction(
        status=BLOCKED, company_name=TAG + 'Blocked Customer',
        name_key=restrictions.normalise(TAG + 'Blocked Customer'),
        reason_category='Payment default / outstanding',
        reason_detail='Outstanding against invoices.',
        recommended_by='SYS', approved_by='SYS',
        recommended_at=datetime.utcnow(), approved_at=datetime.utcnow())
    row.aliases = []; row.domains = []; row.emails = []
    row.business_units = []
    db.session.add(row)
    db.session.commit()
    restrictions.cache_clear()
    try:
        resp = tms('post', '/leads', json={'company': TAG + 'Blocked Customer'})
        assert resp.status_code == 403
        assert resp.get_json()['error']['code'] == 'client_blocked'
        assert Lead.query.filter_by(company=TAG + 'Blocked Customer').count() == 0
    finally:
        ClientRestriction.query.filter_by(id=row.id).delete()
        db.session.commit()
        restrictions.cache_clear()


# ── idempotency ──────────────────────────────────────────────────────
def test_the_same_key_twice_does_the_work_once(tms):
    """The TMS cannot know whether a request that timed out was
    applied, so it has to be free to ask again."""
    key = {'Idempotency-Key': 'tms-create-0001'}
    first = tms('post', '/leads', json={'company': TAG + 'Retried Customer'},
                headers=dict(key))
    second = tms('post', '/leads', json={'company': TAG + 'Retried Customer'},
                 headers=dict(key))
    assert first.status_code == 201
    assert second.get_json().get('replayed') is True
    assert second.get_json()['lead']['crm_lead_id'] == \
        first.get_json()['lead']['crm_lead_id']
    assert Lead.query.filter_by(company=TAG + 'Retried Customer').count() == 1


def test_without_a_key_a_second_call_is_a_second_request(tms):
    tms('post', '/leads', json={'company': TAG + 'Twice Customer'})
    tms('post', '/leads', json={'company': TAG + 'Twice Customer'})
    assert Lead.query.filter_by(company=TAG + 'Twice Customer').count() == 2, (
        'without an idempotency key the CRM must not guess at intent')


# ── linking ──────────────────────────────────────────────────────────
def test_a_lead_can_be_linked_to_a_tms_project(tms):
    lead = Lead(company=TAG + 'Linkable Customer', stage='New')
    db.session.add(lead)
    db.session.commit()

    resp = tms('post', f'/leads/{lead.id}/link-tms',
               json={'tms_project_id': 'TMS-PRJ-77'})
    assert resp.status_code == 201
    body = resp.get_json()
    assert body['created'] and body['link']['tms_project_id'] == 'TMS-PRJ-77'


def test_linking_the_same_pair_twice_makes_one_link(tms):
    lead = Lead(company=TAG + 'Linkable Customer', stage='New')
    db.session.add(lead)
    db.session.commit()
    for _ in range(2):
        tms('post', f'/leads/{lead.id}/link-tms',
            json={'tms_project_id': 'TMS-PRJ-88'})
    assert CrmTmsLink.query.filter_by(crm_lead_id=lead.id).count() == 1


def test_a_lead_is_findable_by_its_tms_project(tms):
    lead = Lead(company=TAG + 'Findable Customer', stage='New')
    db.session.add(lead)
    db.session.commit()
    tms('post', f'/leads/{lead.id}/link-tms',
        json={'tms_project_id': 'TMS-PRJ-99'})
    body = tms('get', '/leads?tms_project_id=TMS-PRJ-99').get_json()
    assert body['found'] and body['lead']['crm_lead_id'] == lead.id


def test_linking_an_unknown_lead_is_a_clear_refusal(tms):
    resp = tms('post', '/leads/99999123/link-tms',
               json={'tms_project_id': 'TMS-PRJ-1'})
    assert resp.status_code == 404
    assert resp.get_json()['error']['code'] == 'not_found'


def test_a_link_needs_an_identifier_on_both_sides(tms):
    lead = Lead(company=TAG + 'Linkable Customer', stage='New')
    db.session.add(lead)
    db.session.commit()
    resp = tms('post', f'/leads/{lead.id}/link-tms', json={})
    assert resp.status_code == 400
    assert resp.get_json()['error']['code'] == 'missing_tms_id'


# ── the log ──────────────────────────────────────────────────────────
def test_every_call_is_logged_with_both_identifiers(tms):
    lead = Lead(company=TAG + 'Logged Customer', stage='New')
    db.session.add(lead)
    db.session.commit()
    tms('post', f'/leads/{lead.id}/link-tms',
        json={'tms_project_id': 'TMS-PRJ-55'})
    row = (IntegrationLog.query
           .filter_by(direction='inbound', status='ok')
           .order_by(IntegrationLog.id.desc()).first())
    assert row.crm_object_id == str(lead.id)
    assert row.tms_object_id == 'TMS-PRJ-55'
    assert row.caller == 'procam-tms'
    assert row.request_id


def test_a_refusal_is_logged_too(tms):
    tms('get', '/ping', headers={'Authorization': 'Bearer wrong'})
    row = (IntegrationLog.query.filter_by(status='refused')
           .order_by(IntegrationLog.id.desc()).first())
    assert row is not None and row.status_code == 401


def test_the_log_never_keeps_the_token(tms):
    tms('post', '/accounts', json={'name': TAG + 'Scrubbed Co',
                                   'token': 'should-not-be-stored'})
    rows = IntegrationLog.query.all()
    blob = ' '.join((r.request_summary or '') + (r.response_summary or '')
                    for r in rows)
    assert TOKEN not in blob
    assert 'should-not-be-stored' not in blob


# ── CRM → TMS ────────────────────────────────────────────────────────
def test_an_event_is_recorded_as_owed_before_anything_is_sent(world):
    """notify_tms no longer means "delivered". It means the TMS is
    owed this event, and the worker will keep trying until it has it
    — because the TMS cannot discover a lead.won it never received."""
    from app.models.integration import WebhookOutbox

    assert integ.notify_tms('lead.won', {'crm_lead_id': 1},
                            crm_object_type='Lead', crm_object_id=1) is True
    row = WebhookOutbox.query.filter_by(event_type='lead.won').one()
    assert row.status == 'queued'
    assert row.event_id == 'lead.won:1'
    assert row.attempts == 0


def test_the_same_event_is_owed_once(world):
    from app.models.integration import WebhookOutbox

    for _ in range(3):
        assert integ.notify_tms('lead.won', {'crm_lead_id': 7},
                                crm_object_type='Lead', crm_object_id=7)
    assert WebhookOutbox.query.filter_by(event_id='lead.won:7').count() == 1


def test_an_event_is_queued_even_with_no_destination_configured(world,
                                                                monkeypatch):
    """Queued, not dropped. The TMS is owed it whether or not anyone
    has configured where to send it yet."""
    from app.models.integration import WebhookOutbox

    monkeypatch.delenv('TMS_WEBHOOK_URL', raising=False)
    assert integ.notify_tms('quote.won', {'crm_quote_id': 3},
                            crm_object_type='Quote', crm_object_id=3)
    row = WebhookOutbox.query.filter_by(event_type='quote.won').one()
    assert row.status == 'queued'
